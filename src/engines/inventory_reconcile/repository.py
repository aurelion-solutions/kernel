# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure database helpers for reconciliation slice.

Rules:
- No logging, no events, no business logic.
- ``session.flush()`` only — caller owns the transaction boundary (commit).
- Pattern: ``session.add`` / ``session.add_all`` + ``await session.flush()``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
    ReconciliationRun,
    ReconciliationRunStatus,
)

# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset(
    {
        ReconciliationRunStatus.pending_apply,
        ReconciliationRunStatus.failed,
        ReconciliationRunStatus.applied,
        ReconciliationRunStatus.partially_applied,
        ReconciliationRunStatus.discarded,
        ReconciliationRunStatus.dry_run_completed,
    }
)


@dataclass(frozen=True)
class RunCounts:
    """Immutable count snapshot for a completed reconciliation run."""

    created: int = field(default=0)
    updated: int = field(default=0)
    revoked: int = field(default=0)
    unchanged: int = field(default=0)


# ---------------------------------------------------------------------------
# Repository functions
# ---------------------------------------------------------------------------


async def create_run(
    session: AsyncSession,
    *,
    application_id: UUID | None,
    entity_type: ReconciliationEntityType = ReconciliationEntityType.access_fact,
    observed_batch_id: UUID | None = None,
    observed_snapshot_id: int | None = None,
    current_snapshot_id: int | None = None,
) -> ReconciliationRun:
    """Insert a new ``ReconciliationRun`` with ``status=running``.

    Flushes but does not commit — caller owns the transaction boundary.
    """
    run = ReconciliationRun(
        application_id=application_id,
        entity_type=entity_type,
        observed_batch_id=observed_batch_id,
        observed_snapshot_id=observed_snapshot_id,
        current_snapshot_id=current_snapshot_id,
        status=ReconciliationRunStatus.running,
        started_at=datetime.now(UTC),
    )
    session.add(run)
    await session.flush()
    return run


async def get_run(
    session: AsyncSession,
    run_id: UUID,
) -> ReconciliationRun | None:
    """Return the run with the given id, or ``None`` if it does not exist."""
    result = await session.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id))
    return result.scalar_one_or_none()


async def update_run_status(
    session: AsyncSession,
    run_id: UUID,
    *,
    status: ReconciliationRunStatus,
    counts: RunCounts | None = None,
    error: str | None = None,
) -> None:
    """Update ``status`` (and optionally counts / error) on an existing run.

    Sets ``finished_at`` automatically for terminal statuses.
    Flushes but does not commit.
    """
    result = await session.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id))
    run = result.scalar_one()

    run.status = status

    if status in _TERMINAL_STATUSES:
        run.finished_at = datetime.now(UTC)

    if counts is not None:
        run.created_count = counts.created
        run.updated_count = counts.updated
        run.revoked_count = counts.revoked
        run.unchanged_count = counts.unchanged

    if error is not None:
        run.error = error

    await session.flush()


async def bulk_insert_delta_items(
    session: AsyncSession,
    items: Sequence[ReconciliationDeltaItem],
) -> None:
    """Persist a batch of delta items via SQLAlchemy unit-of-work.

    Uses ``session.add_all`` + ``await session.flush()`` — single round-trip
    via SQLAlchemy batching.  Matches the ``add_all`` pattern used throughout
    the codebase (e.g. ``inventory/access_facts/repository.py``).

    Caller owns the transaction boundary; does not commit.
    """
    session.add_all(items)
    await session.flush()


async def list_delta_items(
    session: AsyncSession,
    run_id: UUID,
    *,
    status: ReconciliationDeltaItemStatus | None = None,
    limit: int = 100,
    cursor: tuple[datetime, UUID] | None = None,
) -> list[ReconciliationDeltaItem]:
    """Return delta items for a run using keyset pagination on ``(created_at, id)``.

    ``status`` filter is optional.
    ``cursor`` is a ``(created_at, id)`` tuple from the last item of the previous page.
    Fetches ``limit + 1`` rows so the caller can detect whether a next page exists.
    """
    stmt = select(ReconciliationDeltaItem).where(ReconciliationDeltaItem.reconciliation_run_id == run_id)

    if status is not None:
        stmt = stmt.where(ReconciliationDeltaItem.status == status)

    if cursor is not None:
        cursor_ts, cursor_id = cursor
        # Keyset: rows strictly after (cursor_ts, cursor_id)
        stmt = stmt.where(
            (ReconciliationDeltaItem.created_at > cursor_ts)
            | ((ReconciliationDeltaItem.created_at == cursor_ts) & (ReconciliationDeltaItem.id > cursor_id))
        )

    stmt = stmt.order_by(ReconciliationDeltaItem.created_at, ReconciliationDeltaItem.id).limit(limit + 1)

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def list_delta_items_cross_run(
    session: AsyncSession,
    *,
    status: ReconciliationDeltaItemStatus | None = None,
    application_id: UUID | None = None,
    entity_type: ReconciliationEntityType | None = None,
    subject_id: UUID | None = None,
    account_id: UUID | None = None,
    resource_id: UUID | None = None,
    operation: ReconciliationDeltaOperation | None = None,
    limit: int = 50,
    cursor: tuple[datetime, UUID] | None = None,
) -> list[tuple[ReconciliationDeltaItem, UUID | None]]:
    """Return delta items across all runs using keyset pagination on ``(created_at, id)``.

    Joins ``ReconciliationRun`` when ``application_id`` filter is supplied or to
    carry ``run.application_id`` in the result.  Always returns
    ``(delta_item, application_id)`` tuples.

    Fetches ``limit + 1`` rows so the caller can detect whether a next page exists.
    """
    stmt = select(ReconciliationDeltaItem, ReconciliationRun.application_id).join(
        ReconciliationRun, ReconciliationDeltaItem.reconciliation_run_id == ReconciliationRun.id
    )

    if status is not None:
        stmt = stmt.where(ReconciliationDeltaItem.status == status)
    if application_id is not None:
        stmt = stmt.where(ReconciliationRun.application_id == application_id)
    if entity_type is not None:
        stmt = stmt.where(ReconciliationDeltaItem.entity_type == entity_type)
    if subject_id is not None:
        stmt = stmt.where(ReconciliationDeltaItem.subject_id == subject_id)
    if account_id is not None:
        stmt = stmt.where(ReconciliationDeltaItem.account_id == account_id)
    if resource_id is not None:
        stmt = stmt.where(ReconciliationDeltaItem.resource_id == resource_id)
    if operation is not None:
        stmt = stmt.where(ReconciliationDeltaItem.operation == operation)

    if cursor is not None:
        cursor_ts, cursor_id = cursor
        stmt = stmt.where(
            (ReconciliationDeltaItem.created_at > cursor_ts)
            | ((ReconciliationDeltaItem.created_at == cursor_ts) & (ReconciliationDeltaItem.id > cursor_id))
        )

    stmt = stmt.order_by(ReconciliationDeltaItem.created_at, ReconciliationDeltaItem.id).limit(limit + 1)

    result = await session.execute(stmt)
    return [(row[0], row[1]) for row in result.all()]


async def count_delta_items_cross_run(
    session: AsyncSession,
    *,
    status: ReconciliationDeltaItemStatus | None = None,
    application_id: UUID | None = None,
    entity_type: ReconciliationEntityType | None = None,
    subject_id: UUID | None = None,
    account_id: UUID | None = None,
    resource_id: UUID | None = None,
    operation: ReconciliationDeltaOperation | None = None,
) -> int:
    """Return total count of delta items matching the given filters across all runs."""
    stmt = select(func.count(ReconciliationDeltaItem.id)).join(
        ReconciliationRun, ReconciliationDeltaItem.reconciliation_run_id == ReconciliationRun.id
    )

    if status is not None:
        stmt = stmt.where(ReconciliationDeltaItem.status == status)
    if application_id is not None:
        stmt = stmt.where(ReconciliationRun.application_id == application_id)
    if entity_type is not None:
        stmt = stmt.where(ReconciliationDeltaItem.entity_type == entity_type)
    if subject_id is not None:
        stmt = stmt.where(ReconciliationDeltaItem.subject_id == subject_id)
    if account_id is not None:
        stmt = stmt.where(ReconciliationDeltaItem.account_id == account_id)
    if resource_id is not None:
        stmt = stmt.where(ReconciliationDeltaItem.resource_id == resource_id)
    if operation is not None:
        stmt = stmt.where(ReconciliationDeltaItem.operation == operation)

    result = await session.execute(stmt)
    return result.scalar_one()

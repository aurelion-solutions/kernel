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

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
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


async def bulk_approve_run_pending_items(
    session: AsyncSession,
    reconciliation_run_id: UUID,
) -> int:
    """Set all ``pending`` delta items for a run to ``approved``.

    Returns the number of rows updated.
    Flushes but does not commit — caller owns the transaction boundary.
    """
    stmt = (
        update(ReconciliationDeltaItem)
        .where(ReconciliationDeltaItem.reconciliation_run_id == reconciliation_run_id)
        .where(ReconciliationDeltaItem.status == ReconciliationDeltaItemStatus.pending)
        .values(status=ReconciliationDeltaItemStatus.approved)
        .execution_options(synchronize_session='fetch')
    )
    result = await session.execute(stmt)
    await session.flush()
    return result.rowcount  # type: ignore[return-value]


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

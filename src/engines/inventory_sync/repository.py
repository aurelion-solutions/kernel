# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure database helpers for the sync_apply slice.

Rules:
- No logging, no events, no business logic.
- ``session.flush()`` only — caller owns the transaction boundary (commit).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationRun,
)
from src.engines.inventory_sync.models import (
    SyncApplyResult,
    SyncApplyRun,
    SyncApplyRunMode,
    SyncApplyRunStatus,
)


async def create_apply_run(
    session: AsyncSession,
    *,
    reconciliation_run_id: UUID,
    mode: SyncApplyRunMode,
    requested_by: str | None = None,
) -> SyncApplyRun:
    """Insert a new ``SyncApplyRun`` with ``status=running``.

    Flushes but does not commit — caller owns the transaction boundary.
    """
    run = SyncApplyRun(
        reconciliation_run_id=reconciliation_run_id,
        mode=mode,
        status=SyncApplyRunStatus.running,
        started_at=datetime.now(UTC),
        requested_by=requested_by,
    )
    session.add(run)
    await session.flush()
    return run


async def get_apply_run(
    session: AsyncSession,
    apply_run_id: UUID,
) -> SyncApplyRun | None:
    """Return the apply run with the given id, or ``None``."""
    result = await session.execute(select(SyncApplyRun).where(SyncApplyRun.id == apply_run_id))
    return result.scalar_one_or_none()


async def get_active_apply_run_for_reconciliation(
    session: AsyncSession,
    reconciliation_run_id: UUID,
) -> SyncApplyRun | None:
    """Return a blocking apply run for a reconciliation run.

    ``running``, ``completed``, and ``partially_applied`` runs are all treated
    as blocking — re-applying to an already-completed reconciliation run is not
    allowed (matches service.py docstring contract and test Stage-6 expectation).
    """
    active_statuses = [
        SyncApplyRunStatus.running,
        SyncApplyRunStatus.completed,
        SyncApplyRunStatus.partially_applied,
    ]
    result = await session.execute(
        select(SyncApplyRun)
        .where(SyncApplyRun.reconciliation_run_id == reconciliation_run_id)
        .where(SyncApplyRun.status.in_(active_statuses))
        .limit(1)
    )
    return result.scalar_one_or_none()


async def update_apply_run_status(
    session: AsyncSession,
    apply_run_id: UUID,
    *,
    status: SyncApplyRunStatus,
    applied_count: int,
    failed_count: int,
    error: str | None = None,
) -> None:
    """Update status, counts, and optionally error on an apply run.

    Sets ``finished_at`` on terminal statuses. Flushes but does not commit.
    """
    result = await session.execute(select(SyncApplyRun).where(SyncApplyRun.id == apply_run_id))
    run = result.scalar_one()

    run.status = status
    run.applied_count = applied_count
    run.failed_count = failed_count

    terminal = {SyncApplyRunStatus.completed, SyncApplyRunStatus.failed, SyncApplyRunStatus.partially_applied}
    if status in terminal:
        run.finished_at = datetime.now(UTC)

    if error is not None:
        run.error = error

    await session.flush()


async def list_pending_delta_items(
    session: AsyncSession,
    reconciliation_run_id: UUID,
    *,
    item_ids: list[UUID] | None = None,
) -> list[ReconciliationDeltaItem]:
    """Return ``approved`` delta items for the given reconciliation run.

    When ``item_ids`` is provided, only items whose id appears in that list
    are returned (``selected_items`` mode).
    """
    stmt = (
        select(ReconciliationDeltaItem)
        .where(ReconciliationDeltaItem.reconciliation_run_id == reconciliation_run_id)
        .where(ReconciliationDeltaItem.status == ReconciliationDeltaItemStatus.approved)
    )
    if item_ids is not None:
        stmt = stmt.where(ReconciliationDeltaItem.id.in_(item_ids))

    result = await session.execute(stmt)
    return list(result.scalars().all())


async def bulk_insert_results(
    session: AsyncSession,
    results: Sequence[SyncApplyResult],
) -> None:
    """Persist a batch of ``SyncApplyResult`` rows.

    Uses ``session.add_all`` + ``await session.flush()``.
    Caller owns the transaction boundary.
    """
    session.add_all(results)
    await session.flush()


async def mark_delta_items_applied(
    session: AsyncSession,
    item_ids: Sequence[UUID],
    *,
    applied_at: datetime,
) -> None:
    """Set ``status=applied`` and ``applied_at`` on the given delta items.

    Flushes but does not commit.
    Lives here rather than in ``reconciliation/repository.py`` because this
    operation is owned by the apply side, not the reconciliation pipeline.
    """
    if not item_ids:
        return

    items_result = await session.execute(
        select(ReconciliationDeltaItem).where(ReconciliationDeltaItem.id.in_(item_ids))
    )
    for item in items_result.scalars().all():
        item.status = ReconciliationDeltaItemStatus.applied
        item.applied_at = applied_at

    await session.flush()


async def get_reconciliation_run(
    session: AsyncSession,
    reconciliation_run_id: UUID,
) -> ReconciliationRun | None:
    """Return the reconciliation run with the given id, or ``None``."""
    result = await session.execute(select(ReconciliationRun).where(ReconciliationRun.id == reconciliation_run_id))
    return result.scalar_one_or_none()


async def update_reconciliation_run_status(
    session: AsyncSession,
    reconciliation_run_id: UUID,
    *,
    status: str,
) -> None:
    """Update reconciliation run status after apply.

    Flushes but does not commit.
    """
    from src.engines.inventory_reconcile.models import ReconciliationRunStatus

    result = await session.execute(select(ReconciliationRun).where(ReconciliationRun.id == reconciliation_run_id))
    run = result.scalar_one_or_none()
    if run is not None:
        run.status = ReconciliationRunStatus(status)
        run.finished_at = datetime.now(UTC)
        await session.flush()


async def bulk_approve_run_pending_items(
    session: AsyncSession,
    reconciliation_run_id: UUID,
) -> int:
    """Set all ``pending`` delta items for a run to ``approved``.

    Returns rowcount. Flushes; caller owns commit.

    Lives here (sync_apply) rather than in reconciliation/repository because
    this promotion is owned by the apply side — bulk-approval is part of the
    apply workflow, not the reconciliation pipeline. Sibling rationale to
    ``mark_delta_items_applied`` (see line 155 docstring).
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


async def get_non_approved_items(
    session: AsyncSession,
    reconciliation_run_id: UUID,
    item_ids: list[UUID],
) -> list[ReconciliationDeltaItem]:
    """Return items from ``item_ids`` that are NOT in ``approved`` status.

    Used for ``selected_items`` validation.
    """
    result = await session.execute(
        select(ReconciliationDeltaItem)
        .where(ReconciliationDeltaItem.reconciliation_run_id == reconciliation_run_id)
        .where(ReconciliationDeltaItem.id.in_(item_ids))
        .where(ReconciliationDeltaItem.status != ReconciliationDeltaItemStatus.approved)
    )
    return list(result.scalars().all())

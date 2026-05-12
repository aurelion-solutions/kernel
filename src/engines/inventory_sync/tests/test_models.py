# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ORM round-trip tests for sync_apply models.

Tests:
- Insert SyncApplyRun + two SyncApplyResult rows, verify FK linkage and enum
  coercion.
- Verify ON DELETE CASCADE: deleting the run removes both results.
- Verify soft lake ref: fact_id round-trips as a plain UUID with no FK
  constraint.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.inventory_sync.models import (
    SyncApplyResult,
    SyncApplyResultStatus,
    SyncApplyRun,
    SyncApplyRunMode,
    SyncApplyRunStatus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_application(session: AsyncSession) -> uuid.UUID:
    from src.platform.applications.models import Application

    app = Application(
        name=f'sync-apply-test-{uuid.uuid4()}',
        code=f'sa-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id  # type: ignore[return-value]


async def _make_reconciliation_run(session: AsyncSession, application_id: uuid.UUID) -> uuid.UUID:
    from src.engines.inventory_reconcile.models import ReconciliationRun, ReconciliationRunStatus

    run = ReconciliationRun(
        application_id=application_id,
        status=ReconciliationRunStatus.pending_apply,
    )
    session.add(run)
    await session.flush()
    return run.id  # type: ignore[return-value]


async def _make_delta_item(
    session: AsyncSession,
    reconciliation_run_id: uuid.UUID,
) -> uuid.UUID:
    from src.engines.inventory_reconcile.models import (
        ReconciliationDeltaItem,
        ReconciliationDeltaOperation,
    )

    item = ReconciliationDeltaItem(
        reconciliation_run_id=reconciliation_run_id,
        operation=ReconciliationDeltaOperation.create,
        natural_key_hash=uuid.uuid4().hex * 2,  # 64 hex chars
        subject_id=uuid.uuid4(),
        resource_id=uuid.uuid4(),
        action_id=1,
        effect='allow',
    )
    session.add(item)
    await session.flush()
    return item.id  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_sync_apply_run_round_trip(session_factory) -> None:
    """Insert SyncApplyRun and verify field round-trip including enum coercion."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        recon_run_id = await _make_reconciliation_run(session, app_id)

        run = SyncApplyRun(
            reconciliation_run_id=recon_run_id,
            mode=SyncApplyRunMode.auto_apply,
            status=SyncApplyRunStatus.running,
        )
        session.add(run)
        await session.flush()
        run_id = run.id

        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(SyncApplyRun, run_id)
        assert fetched is not None
        assert fetched.reconciliation_run_id == recon_run_id
        assert fetched.status == SyncApplyRunStatus.running
        assert fetched.mode == SyncApplyRunMode.auto_apply
        assert fetched.applied_count == 0
        assert fetched.failed_count == 0
        assert fetched.created_at is not None
        assert fetched.started_at is None
        assert fetched.finished_at is None
        assert fetched.requested_by is None
        assert fetched.error is None


async def test_sync_apply_result_round_trip(session_factory) -> None:
    """Insert SyncApplyResult and verify FK linkage, enum coercion, and soft
    lake ref fact_id stored as plain UUID."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        recon_run_id = await _make_reconciliation_run(session, app_id)
        delta_item_id = await _make_delta_item(session, recon_run_id)

        run = SyncApplyRun(
            reconciliation_run_id=recon_run_id,
            mode=SyncApplyRunMode.auto_apply,
        )
        session.add(run)
        await session.flush()

        fact_id = uuid.uuid4()
        result = SyncApplyResult(
            sync_apply_run_id=run.id,
            delta_item_id=delta_item_id,
            status=SyncApplyResultStatus.applied,
            fact_id=fact_id,
            snapshot_id=12345,
        )
        session.add(result)
        await session.flush()
        result_id = result.id
        run_id = run.id

        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(SyncApplyResult, result_id)
        assert fetched is not None
        assert fetched.sync_apply_run_id == run_id
        assert fetched.delta_item_id == delta_item_id
        assert fetched.status == SyncApplyResultStatus.applied
        assert fetched.fact_id == fact_id
        assert fetched.snapshot_id == 12345
        assert fetched.created_at is not None
        assert fetched.error is None


async def test_cascade_delete_results_on_run_delete(session_factory) -> None:
    """Deleting a SyncApplyRun cascades to remove both SyncApplyResult rows."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        recon_run_id = await _make_reconciliation_run(session, app_id)
        delta_item_id_1 = await _make_delta_item(session, recon_run_id)
        delta_item_id_2 = await _make_delta_item(session, recon_run_id)

        run = SyncApplyRun(
            reconciliation_run_id=recon_run_id,
            mode=SyncApplyRunMode.manual_apply,
        )
        session.add(run)
        await session.flush()
        run_id = run.id

        result1 = SyncApplyResult(
            sync_apply_run_id=run_id,
            delta_item_id=delta_item_id_1,
            status=SyncApplyResultStatus.applied,
        )
        result2 = SyncApplyResult(
            sync_apply_run_id=run_id,
            delta_item_id=delta_item_id_2,
            status=SyncApplyResultStatus.failed,
            error='something went wrong',
        )
        session.add_all([result1, result2])
        await session.flush()
        result1_id = result1.id
        result2_id = result2.id

        await session.commit()

    # Verify both results exist
    async with session_factory() as session:
        r1 = await session.get(SyncApplyResult, result1_id)
        r2 = await session.get(SyncApplyResult, result2_id)
        assert r1 is not None
        assert r2 is not None

    # Delete the run — results should cascade
    async with session_factory() as session:
        run = await session.get(SyncApplyRun, run_id)
        assert run is not None
        await session.delete(run)
        await session.commit()

    # Both results must be gone
    async with session_factory() as session:
        r1_after = await session.get(SyncApplyResult, result1_id)
        r2_after = await session.get(SyncApplyResult, result2_id)
        assert r1_after is None, 'SyncApplyResult 1 not deleted by cascade'
        assert r2_after is None, 'SyncApplyResult 2 not deleted by cascade'


async def test_fact_id_is_soft_lake_ref(session_factory) -> None:
    """fact_id round-trips as a UUID without any FK constraint.

    An arbitrary UUID that does not point at any real Iceberg row must be
    accepted by the database — the soft lake reference contract has no
    DB-enforced integrity.
    """
    phantom_fact_id = uuid.UUID('00000000-dead-beef-0000-000000000000')

    async with session_factory() as session:
        app_id = await _make_application(session)
        recon_run_id = await _make_reconciliation_run(session, app_id)
        delta_item_id = await _make_delta_item(session, recon_run_id)

        run = SyncApplyRun(
            reconciliation_run_id=recon_run_id,
            mode=SyncApplyRunMode.dry_run,
        )
        session.add(run)
        await session.flush()

        result = SyncApplyResult(
            sync_apply_run_id=run.id,
            delta_item_id=delta_item_id,
            status=SyncApplyResultStatus.skipped,
            fact_id=phantom_fact_id,
        )
        session.add(result)
        await session.flush()
        result_id = result.id
        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(SyncApplyResult, result_id)
        assert fetched is not None
        assert fetched.fact_id == phantom_fact_id


async def test_query_results_by_run(session_factory) -> None:
    """Query SyncApplyResult rows filtered by sync_apply_run_id."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        recon_run_id = await _make_reconciliation_run(session, app_id)
        delta_item_id_1 = await _make_delta_item(session, recon_run_id)
        delta_item_id_2 = await _make_delta_item(session, recon_run_id)

        run = SyncApplyRun(
            reconciliation_run_id=recon_run_id,
            mode=SyncApplyRunMode.auto_apply,
        )
        session.add(run)
        await session.flush()

        result1 = SyncApplyResult(
            sync_apply_run_id=run.id,
            delta_item_id=delta_item_id_1,
            status=SyncApplyResultStatus.applied,
        )
        result2 = SyncApplyResult(
            sync_apply_run_id=run.id,
            delta_item_id=delta_item_id_2,
            status=SyncApplyResultStatus.failed,
        )
        session.add_all([result1, result2])
        await session.flush()
        run_id = run.id
        await session.commit()

    async with session_factory() as session:
        rows = (
            (await session.execute(select(SyncApplyResult).where(SyncApplyResult.sync_apply_run_id == run_id)))
            .scalars()
            .all()
        )
        assert len(rows) == 2
        statuses = {r.status for r in rows}
        assert SyncApplyResultStatus.applied in statuses
        assert SyncApplyResultStatus.failed in statuses

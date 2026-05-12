# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Round-trip tests for engines.reconciliation.repository."""

from __future__ import annotations

import uuid

import pytest
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationRunStatus,
)
from src.engines.inventory_reconcile.repository import (
    RunCounts,
    bulk_insert_delta_items,
    create_run,
    list_delta_items,
    update_run_status,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_application(session) -> uuid.UUID:
    from src.platform.applications.models import Application

    app = Application(
        name=f'repo-test-{uuid.uuid4()}',
        code=f'rt-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


def _make_delta_item(run_id: uuid.UUID, **kwargs) -> ReconciliationDeltaItem:
    defaults = {
        'reconciliation_run_id': run_id,
        'operation': ReconciliationDeltaOperation.create,
        'natural_key_hash': uuid.uuid4().hex * 2,  # 64 hex chars
        'subject_id': uuid.uuid4(),
        'account_id': None,
        'resource_id': uuid.uuid4(),
        'action_id': 1,
        'effect': 'allow',
    }
    defaults.update(kwargs)
    return ReconciliationDeltaItem(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_run_happy_path(session_factory):
    """create_run inserts row with status=running and correct snapshot ids."""
    app_id = None
    async with session_factory() as session:
        app_id = await _make_application(session)

        run = await create_run(
            session,
            application_id=app_id,
            observed_snapshot_id=42,
            current_snapshot_id=99,
        )
        await session.commit()

        run_id = run.id

    async with session_factory() as session:
        from sqlalchemy import select
        from src.engines.inventory_reconcile.models import ReconciliationRun

        result = await session.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id))
        fetched = result.scalar_one()

    assert fetched.status == ReconciliationRunStatus.running
    assert fetched.observed_snapshot_id == 42
    assert fetched.current_snapshot_id == 99
    assert fetched.finished_at is None
    assert fetched.started_at is not None


@pytest.mark.asyncio
async def test_update_run_status_running_to_pending_apply(session_factory):
    """update_run_status running → pending_apply sets counts + finished_at."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        run = await create_run(session, application_id=app_id)
        run_id = run.id

        await update_run_status(
            session,
            run_id,
            status=ReconciliationRunStatus.pending_apply,
            counts=RunCounts(created=5, updated=3, revoked=2, unchanged=10),
        )
        await session.commit()

    async with session_factory() as session:
        from sqlalchemy import select
        from src.engines.inventory_reconcile.models import ReconciliationRun

        result = await session.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id))
        fetched = result.scalar_one()

    assert fetched.status == ReconciliationRunStatus.pending_apply
    assert fetched.created_count == 5
    assert fetched.updated_count == 3
    assert fetched.revoked_count == 2
    assert fetched.unchanged_count == 10
    assert fetched.finished_at is not None


@pytest.mark.asyncio
async def test_update_run_status_running_to_failed(session_factory):
    """update_run_status running → failed sets error + finished_at."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        run = await create_run(session, application_id=app_id)
        run_id = run.id

        await update_run_status(
            session,
            run_id,
            status=ReconciliationRunStatus.failed,
            error='something exploded',
        )
        await session.commit()

    async with session_factory() as session:
        from sqlalchemy import select
        from src.engines.inventory_reconcile.models import ReconciliationRun

        result = await session.execute(select(ReconciliationRun).where(ReconciliationRun.id == run_id))
        fetched = result.scalar_one()

    assert fetched.status == ReconciliationRunStatus.failed
    assert fetched.error == 'something exploded'
    assert fetched.finished_at is not None


@pytest.mark.asyncio
async def test_bulk_insert_delta_items(session_factory):
    """bulk_insert_delta_items persists 50 items in one batch."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        run = await create_run(session, application_id=app_id)
        run_id = run.id

        items = [_make_delta_item(run_id) for _ in range(50)]
        await bulk_insert_delta_items(session, items)
        await session.commit()

    async with session_factory() as session:
        from sqlalchemy import func, select
        from src.engines.inventory_reconcile.models import ReconciliationDeltaItem

        count_result = await session.execute(
            select(func.count()).where(ReconciliationDeltaItem.reconciliation_run_id == run_id)
        )
        count = count_result.scalar_one()

    assert count == 50


@pytest.mark.asyncio
async def test_list_delta_items_pagination(session_factory):
    """list_delta_items keyset pagination: pages do not overlap and cover all rows."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        run = await create_run(session, application_id=app_id)
        run_id = run.id

        items = [_make_delta_item(run_id) for _ in range(30)]
        await bulk_insert_delta_items(session, items)
        await session.commit()

    async with session_factory() as session:
        # Page 1 — no cursor, limit+1 to detect next page
        page1_raw = await list_delta_items(session, run_id, limit=10)
        assert len(page1_raw) == 11  # limit+1 returned → more pages
        page1 = page1_raw[:10]

        # Encode cursor from last item of page 1
        last1 = page1[-1]
        cursor2 = (last1.created_at, last1.id)

        # Page 2
        page2_raw = await list_delta_items(session, run_id, limit=10, cursor=cursor2)
        assert len(page2_raw) == 11
        page2 = page2_raw[:10]

        last2 = page2[-1]
        cursor3 = (last2.created_at, last2.id)

        # Page 3
        page3_raw = await list_delta_items(session, run_id, limit=10, cursor=cursor3)
        # 30 items total; pages 1+2 = 20, so page3 should have exactly 10 items
        assert len(page3_raw) == 10
        page3 = page3_raw

    # Pages must not overlap
    ids_p1 = {item.id for item in page1}
    ids_p2 = {item.id for item in page2}
    ids_p3 = {item.id for item in page3}
    assert not ids_p1 & ids_p2
    assert not ids_p2 & ids_p3
    assert not ids_p1 & ids_p3
    # All 30 rows covered
    assert len(ids_p1 | ids_p2 | ids_p3) == 30


@pytest.mark.asyncio
async def test_list_delta_items_status_filter(session_factory):
    """list_delta_items status filter returns only items with matching status."""
    async with session_factory() as session:
        app_id = await _make_application(session)
        run = await create_run(session, application_id=app_id)
        run_id = run.id

        # 5 pending + 3 approved
        pending_items = [_make_delta_item(run_id) for _ in range(5)]
        approved_items = [_make_delta_item(run_id, status=ReconciliationDeltaItemStatus.approved) for _ in range(3)]
        await bulk_insert_delta_items(session, pending_items + approved_items)
        await session.commit()

    async with session_factory() as session:
        pending = await list_delta_items(session, run_id, status=ReconciliationDeltaItemStatus.pending)
        approved = await list_delta_items(session, run_id, status=ReconciliationDeltaItemStatus.approved)

    assert len(pending) == 5
    assert len(approved) == 3
    assert all(i.status == ReconciliationDeltaItemStatus.pending for i in pending)
    assert all(i.status == ReconciliationDeltaItemStatus.approved for i in approved)

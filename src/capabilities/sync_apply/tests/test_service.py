# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for SyncApplyService.

Strategy:
- Real PostgreSQL session via ``session_factory`` fixture.
- In-process SQLite-backed PyIceberg catalog + tmp-dir warehouse.
- ``denorm_resolver`` is a simple closure (no async DB lookup needed since items
  are pre-built with known data).
- ``EventService`` uses ``CapturingEventService`` to assert event emission.
- ``LogService`` uses ``CapturingLogSink``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import uuid

from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType
import pytest
from src.capabilities.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from src.capabilities.sync_apply.exceptions import (
    SyncApplyAlreadyExecutedError,
    SyncApplyDeltaItemNotApplicableError,
    SyncApplyRunNotFoundError,
)
from src.capabilities.sync_apply.lake_writer import write_run_batch
from src.capabilities.sync_apply.models import SyncApplyRunMode, SyncApplyRunStatus
from src.capabilities.sync_apply.service import SyncApplyService
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSessionFactory
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NOW = datetime.now(tz=UTC)
_NOW_ISO = _NOW.isoformat()
_APP_ID = uuid.UUID('aaaaaaaa-0000-0000-0000-000000000001')
_SUBJECT_ID = uuid.UUID('bbbbbbbb-0000-0000-0000-000000000002')
_RESOURCE_ID = uuid.UUID('cccccccc-0000-0000-0000-000000000003')
_ACTION_ID = 42


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_log_service() -> tuple[LogService, CapturingLogSink]:
    sink = CapturingLogSink()
    return LogService(sink=sink), sink


def _after_json(
    *,
    effect: str = 'allow',
    valid_from: str | None = None,
    observed_at: str | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    return {
        'effect': effect,
        'valid_from': valid_from or _NOW_ISO,
        'observed_at': observed_at or _NOW_ISO,
        'created_at': created_at or _NOW_ISO,
        'valid_until': None,
        'revoked_at': None,
        'latest_batch_id': None,
    }


def _simple_denorm(item: ReconciliationDeltaItem) -> tuple[str, str]:
    return str(_APP_ID), 'employee'


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_catalog() -> Any:  # noqa: ANN401
    reset_catalog_cache_for_tests()
    yield
    reset_catalog_cache_for_tests()


@pytest.fixture
def lake_settings(tmp_path: Path) -> LakeSettings:
    return LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/catalog.db',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
    )


@pytest.fixture
def catalog(lake_settings: LakeSettings) -> Any:  # noqa: ANN401
    """Return catalog with a string-partition normalized.access_facts test table."""
    log, _ = _make_log_service()
    cat = get_catalog(lake_settings, log_service=log)

    for ns in (('raw',), ('normalized',)):
        try:
            cat.create_namespace(ns)
        except Exception:
            pass

    test_schema = Schema(
        NestedField(1, 'id', StringType(), required=True),
        NestedField(2, 'subject_id', StringType(), required=True),
        NestedField(3, 'account_id', StringType(), required=False),
        NestedField(4, 'resource_id', StringType(), required=True),
        NestedField(5, 'action_id', StringType(), required=True),
        NestedField(6, 'effect', StringType(), required=True),
        NestedField(7, 'valid_from', TimestamptzType(), required=True),
        NestedField(8, 'valid_until', TimestamptzType(), required=False),
        NestedField(9, 'is_active', BooleanType(), required=True),
        NestedField(10, 'observed_at', TimestamptzType(), required=True),
        NestedField(11, 'created_at', TimestamptzType(), required=True),
        NestedField(12, 'revoked_at', TimestamptzType(), required=False),
        NestedField(13, 'latest_batch_id', StringType(), required=False),
        NestedField(14, 'application_id_denorm', StringType(), required=True),
        NestedField(15, 'subject_kind_denorm', StringType(), required=True),
        NestedField(16, 'reconciliation_delta_item_id', StringType(), required=True),
        NestedField(17, 'natural_key_hash', StringType(), required=True),
    )
    test_spec = PartitionSpec(
        PartitionField(source_id=15, field_id=1000, transform=IdentityTransform(), name='subject_kind_denorm')
    )
    identifier = ('normalized', 'access_facts')
    try:
        cat.drop_table(identifier)
    except Exception:
        pass
    cat.create_table(identifier, schema=test_schema, partition_spec=test_spec)
    return cat


@pytest.fixture
def lake_session_factory(lake_settings: LakeSettings) -> LakeSessionFactory:
    log, _ = _make_log_service()
    return LakeSessionFactory(settings=lake_settings, log_service=log, pg_dsn=None)


@pytest.fixture
def capturing_events() -> CapturingEventService:
    return CapturingEventService()


@pytest.fixture
def event_service(capturing_events: CapturingEventService) -> EventService:
    return EventService(sink=capturing_events)


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


async def _seed_reconciliation_run(session, *, app_id: uuid.UUID | None = None) -> ReconciliationRun:
    """Create and persist a minimal ReconciliationRun in pending_apply status."""
    from src.platform.applications.models import Application

    if app_id is None:
        app = Application(
            name=f'test-app-{uuid.uuid4()}',
            code=f'app-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.flush()
        app_id = app.id

    run = ReconciliationRun(
        application_id=app_id,
        status=ReconciliationRunStatus.pending_apply,
        started_at=_NOW,
        finished_at=_NOW,
    )
    session.add(run)
    await session.flush()
    return run


async def _seed_delta_items(
    session,
    *,
    run: ReconciliationRun,
    count: int = 1,
    operation: ReconciliationDeltaOperation = ReconciliationDeltaOperation.create,
    status: ReconciliationDeltaItemStatus = ReconciliationDeltaItemStatus.approved,
) -> list[ReconciliationDeltaItem]:
    """Seed delta items for a given run."""
    items = []
    for i in range(count):
        item = ReconciliationDeltaItem(
            reconciliation_run_id=run.id,
            operation=operation,
            natural_key_hash=f'{i:064x}',
            subject_id=_SUBJECT_ID,
            account_id=None,
            resource_id=_RESOURCE_ID,
            action_id=_ACTION_ID,
            effect='allow',
            status=status,
            before_json=None,
            after_json=_after_json(),
        )
        session.add(item)
        items.append(item)
    await session.flush()
    return items


def _make_service(session, lake_session_factory, catalog, event_service) -> SyncApplyService:
    lake_session = lake_session_factory.acquire()
    log, _ = _make_log_service()
    return SyncApplyService(
        session=session,
        lake_session=lake_session,
        catalog=catalog,
        denorm_resolver=_simple_denorm,
        events=event_service,
        logs=log,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_three_create_items_writes_three_facts(
    session_factory,
    catalog: Any,
    lake_session_factory: LakeSessionFactory,
    capturing_events: CapturingEventService,
    event_service: EventService,
) -> None:
    """3 approved create items → 3 results, 3 events, applied_count=3, failed_count=0."""
    async with session_factory() as session:
        run = await _seed_reconciliation_run(session)
        items = await _seed_delta_items(session, run=run, count=3, operation=ReconciliationDeltaOperation.create)

        svc = _make_service(session, lake_session_factory, catalog, event_service)
        resp = await svc.apply(
            reconciliation_run_id=run.id,
            mode=SyncApplyRunMode.auto_apply,
        )
        await session.commit()

    assert resp.applied_count == 3
    assert resp.failed_count == 0
    assert resp.status == SyncApplyRunStatus.completed

    # 3 inventory.access_fact.created events
    created_events = capturing_events.filter_by_type('inventory.access_fact.created')
    assert len(created_events) == 3

    # Each event carries delta_item_id
    item_ids = {str(item.id) for item in items}
    event_delta_ids = {ev.payload['delta_item_id'] for ev in created_events}
    assert event_delta_ids == item_ids


@pytest.mark.asyncio
async def test_apply_dry_run_makes_no_writes(
    session_factory,
    catalog: Any,
    lake_session_factory: LakeSessionFactory,
    capturing_events: CapturingEventService,
    event_service: EventService,
) -> None:
    """mode=dry_run → zero Iceberg snapshots, zero events, all results skipped."""
    async with session_factory() as session:
        run = await _seed_reconciliation_run(session)
        await _seed_delta_items(session, run=run, count=3)

        # Capture snapshot before apply
        iceberg_table = catalog.load_table(('normalized', 'access_facts'))
        snap_before = iceberg_table.current_snapshot()

        svc = _make_service(session, lake_session_factory, catalog, event_service)
        resp = await svc.apply(
            reconciliation_run_id=run.id,
            mode=SyncApplyRunMode.dry_run,
        )
        await session.commit()

    assert resp.applied_count == 0
    assert resp.failed_count == 0
    assert resp.status == SyncApplyRunStatus.completed
    assert resp.snapshot_ids == {}

    # No events
    assert capturing_events.filter_by_type('inventory.access_fact.created') == []
    assert capturing_events.emitted == []

    # No new Iceberg snapshots
    iceberg_table = catalog.load_table(('normalized', 'access_facts'))
    snap_after = iceberg_table.current_snapshot()
    assert snap_before == snap_after


@pytest.mark.asyncio
async def test_apply_recovers_already_written_items(
    session_factory,
    catalog: Any,
    lake_session_factory: LakeSessionFactory,
    capturing_events: CapturingEventService,
    event_service: EventService,
) -> None:
    """Item already in Iceberg (crash scenario) is recovered without duplicate write."""
    async with session_factory() as session:
        run = await _seed_reconciliation_run(session)
        items = await _seed_delta_items(session, run=run, count=1)
        item = items[0]

        # Simulate crashed write: item is in Iceberg but still 'approved' in PG
        log, _ = _make_log_service()
        write_run_batch([item], catalog=catalog, denorm_resolver=_simple_denorm, log_service=log)

        # Capture snapshot count before apply
        iceberg_table = catalog.load_table(('normalized', 'access_facts'))
        snap_before = iceberg_table.current_snapshot()

        svc = _make_service(session, lake_session_factory, catalog, event_service)
        resp = await svc.apply(
            reconciliation_run_id=run.id,
            mode=SyncApplyRunMode.auto_apply,
        )
        await session.commit()

    assert resp.applied_count == 1
    assert resp.failed_count == 0

    # No NEW Iceberg snapshots (recovered path skips write)
    iceberg_table = catalog.load_table(('normalized', 'access_facts'))
    snap_after = iceberg_table.current_snapshot()
    assert snap_before == snap_after

    # Event still emitted for recovered item (user-visible contract)
    created_events = capturing_events.filter_by_type('inventory.access_fact.created')
    assert len(created_events) == 1
    assert created_events[0].payload['delta_item_id'] == str(item.id)


@pytest.mark.asyncio
async def test_apply_unknown_run_raises(
    session_factory,
    catalog: Any,
    lake_session_factory: LakeSessionFactory,
    event_service: EventService,
) -> None:
    """apply with non-existent reconciliation_run_id raises SyncApplyRunNotFoundError."""
    async with session_factory() as session:
        svc = _make_service(session, lake_session_factory, catalog, event_service)
        with pytest.raises(SyncApplyRunNotFoundError):
            await svc.apply(
                reconciliation_run_id=uuid.uuid4(),
                mode=SyncApplyRunMode.auto_apply,
            )


@pytest.mark.asyncio
async def test_apply_twice_raises_already_executed(
    session_factory,
    catalog: Any,
    lake_session_factory: LakeSessionFactory,
    event_service: EventService,
) -> None:
    """Second apply call for same reconciliation_run_id raises SyncApplyAlreadyExecutedError."""
    async with session_factory() as session:
        run = await _seed_reconciliation_run(session)
        await _seed_delta_items(session, run=run, count=1)

        svc = _make_service(session, lake_session_factory, catalog, event_service)
        await svc.apply(reconciliation_run_id=run.id, mode=SyncApplyRunMode.auto_apply)
        await session.flush()

        with pytest.raises(SyncApplyAlreadyExecutedError):
            await svc.apply(reconciliation_run_id=run.id, mode=SyncApplyRunMode.auto_apply)


@pytest.mark.asyncio
async def test_apply_selected_items_filters_correctly(
    session_factory,
    catalog: Any,
    lake_session_factory: LakeSessionFactory,
    capturing_events: CapturingEventService,
    event_service: EventService,
) -> None:
    """selected_items mode applies only the specified item_ids."""
    async with session_factory() as session:
        run = await _seed_reconciliation_run(session)
        items = await _seed_delta_items(session, run=run, count=5)
        selected = [items[0].id, items[2].id]

        svc = _make_service(session, lake_session_factory, catalog, event_service)
        resp = await svc.apply(
            reconciliation_run_id=run.id,
            mode=SyncApplyRunMode.selected_items,
            item_ids=selected,
        )
        await session.commit()

    assert resp.applied_count == 2
    assert resp.failed_count == 0

    # Only 2 events
    assert len(capturing_events.filter_by_type('inventory.access_fact.created')) == 2


@pytest.mark.asyncio
async def test_apply_selected_items_with_non_approved_raises(
    session_factory,
    catalog: Any,
    lake_session_factory: LakeSessionFactory,
    event_service: EventService,
) -> None:
    """selected_items with a pending item raises SyncApplyDeltaItemNotApplicableError."""
    async with session_factory() as session:
        run = await _seed_reconciliation_run(session)
        # One approved, one pending
        approved_items = await _seed_delta_items(
            session, run=run, count=1, status=ReconciliationDeltaItemStatus.approved
        )
        pending_items = await _seed_delta_items(session, run=run, count=1, status=ReconciliationDeltaItemStatus.pending)

        svc = _make_service(session, lake_session_factory, catalog, event_service)
        with pytest.raises(SyncApplyDeltaItemNotApplicableError):
            await svc.apply(
                reconciliation_run_id=run.id,
                mode=SyncApplyRunMode.selected_items,
                item_ids=[approved_items[0].id, pending_items[0].id],
            )

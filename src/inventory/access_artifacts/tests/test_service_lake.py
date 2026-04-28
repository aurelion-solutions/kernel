# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for AccessArtifactService — lake write path (Phase 15 Step 5).

NOTE: Tests use a string-partition test table (artifacts_table_fixture) rather than
the production UUID-partition ``raw.access_artifacts`` table because PyArrow 24 does
not support ``group_by`` on ``extension<arrow.uuid>`` columns, which PyIceberg
requires for writing to UUID-partitioned tables.  The business logic (dedup, tombstone,
gating) is identical regardless of the partition key type.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch
import uuid

from pyiceberg.catalog import Catalog
import pytest
from src.inventory.access_artifacts.service import (
    AccessArtifactBatchItem,
    AccessArtifactBatchTooLargeError,
    AccessArtifactLakeNotConfiguredError,
    AccessArtifactLakeWriteError,
    AccessArtifactService,
    BatchUpsertResult,
)
from src.platform.events.testing import CapturingEventService
from src.platform.lake.config import LakeSettings
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_items(n: int, *, app_id: uuid.UUID | None = None) -> list[AccessArtifactBatchItem]:
    aid = app_id if app_id is not None else uuid.uuid4()
    return [
        AccessArtifactBatchItem(
            application_id=aid,
            artifact_type='sap_role',
            external_id=f'role-{i}',
            payload={'name': f'Role {i}'},
        )
        for i in range(n)
    ]


async def _make_application_id(session: Any) -> uuid.UUID:
    from src.platform.applications.models import Application

    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


# ---------------------------------------------------------------------------
# Test 1: pg gate — upsert_batch routes to SQLAlchemy path, catalog not called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_batch_routes_to_pg_when_gate_pg(
    session_factory: Any,
    lake_settings_pg: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """gate=pg: upsert_batch calls SQLAlchemy repo, catalog never called, events emitted."""
    log_service, _ = capturing_log_service
    mock_catalog = MagicMock()

    capturing_events = CapturingEventService()
    from src.platform.events.service import EventService

    event_service = EventService(sink=capturing_events)

    svc = AccessArtifactService(
        event_service=event_service,
        log_service=log_service,
        lake_settings=lake_settings_pg,
        lake_catalog=mock_catalog,
    )

    async with session_factory() as session:
        app_id = await _make_application_id(session)
        items = _make_items(3, app_id=app_id)
        batch_id = uuid.uuid4()

        with patch(
            'src.inventory.access_artifacts.service.repo_upsert_access_artifact',
            wraps=__import__(
                'src.inventory.access_artifacts.repository',
                fromlist=['upsert_access_artifact'],
            ).upsert_access_artifact,
        ) as mock_repo:
            result = await svc.upsert_batch(
                session,
                items,
                ingest_batch_id=batch_id,
            )
            await session.commit()

    # Catalog was never called
    mock_catalog.load_table.assert_not_called()

    # repo called 3 times
    assert mock_repo.call_count == 3

    # Result has pg backend
    assert result.backend == 'pg'
    assert result.row_count == 3
    assert result.snapshot_id is None

    # Per-row events emitted
    ingested = capturing_events.filter_by_type('inventory.access_artifact.ingested')
    assert len(ingested) == 3


# ---------------------------------------------------------------------------
# Test 2: iceberg gate — upsert_batch routes to Iceberg, SQLAlchemy not called
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_batch_routes_to_iceberg_when_gate_iceberg(
    session_factory: Any,
    lake_settings_iceberg: LakeSettings,
    artifacts_table_fixture: Catalog,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """gate=iceberg: upsert_batch writes to Iceberg, no SQLAlchemy, no events."""
    log_service, _ = capturing_log_service
    catalog = artifacts_table_fixture

    capturing_events = CapturingEventService()
    from src.platform.events.service import EventService

    event_service = EventService(sink=capturing_events)

    svc = AccessArtifactService(
        event_service=event_service,
        log_service=log_service,
        lake_settings=lake_settings_iceberg,
        lake_catalog=catalog,
    )

    app_id = uuid.uuid4()
    items = _make_items(5, app_id=app_id)
    batch_id = uuid.uuid4()

    async with session_factory() as session:
        with patch(
            'src.inventory.access_artifacts.service.repo_upsert_access_artifact',
        ) as mock_repo:
            result = await svc.upsert_batch(
                session,
                items,
                ingest_batch_id=batch_id,
            )

        # SQLAlchemy repo was NEVER called
        mock_repo.assert_not_called()

    # Result carries iceberg backend and a truthy snapshot_id
    assert result.backend == 'iceberg'
    assert result.row_count == 5
    assert result.snapshot_id is not None

    # Verify rows are visible via Iceberg scan
    table = catalog.load_table(('raw', 'access_artifacts'))
    scan_arrow = table.scan().to_arrow()
    assert len(scan_arrow) == 5

    # No per-row events under iceberg gate
    ingested = capturing_events.filter_by_type('inventory.access_artifact.ingested')
    assert len(ingested) == 0


# ---------------------------------------------------------------------------
# Test 3: Iceberg dedup via overwrite — second call retires old rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_batch_dedupes_on_iceberg_via_overwrite_filter(
    session_factory: Any,
    lake_settings_iceberg: LakeSettings,
    artifacts_table_fixture: Catalog,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """Second iceberg upsert retires old rows for overlapping external_ids."""
    log_service, _ = capturing_log_service
    catalog = artifacts_table_fixture

    svc = AccessArtifactService(
        log_service=log_service,
        lake_settings=lake_settings_iceberg,
        lake_catalog=catalog,
    )

    app_id = uuid.uuid4()

    # First batch: A, B, C
    first_items = [
        AccessArtifactBatchItem(application_id=app_id, artifact_type='sap_role', external_id='A', payload={'v': 1}),
        AccessArtifactBatchItem(application_id=app_id, artifact_type='sap_role', external_id='B', payload={'v': 1}),
        AccessArtifactBatchItem(application_id=app_id, artifact_type='sap_role', external_id='C', payload={'v': 1}),
    ]
    async with session_factory() as session:
        await svc.upsert_batch(session, first_items, ingest_batch_id=uuid.uuid4())

    # Second batch: B (modified), C (modified), D (new)
    second_items = [
        AccessArtifactBatchItem(application_id=app_id, artifact_type='sap_role', external_id='B', payload={'v': 2}),
        AccessArtifactBatchItem(application_id=app_id, artifact_type='sap_role', external_id='C', payload={'v': 2}),
        AccessArtifactBatchItem(application_id=app_id, artifact_type='sap_role', external_id='D', payload={'v': 1}),
    ]
    async with session_factory() as session:
        await svc.upsert_batch(session, second_items, ingest_batch_id=uuid.uuid4())

    # Verify final state
    table = catalog.load_table(('raw', 'access_artifacts'))
    all_rows = table.scan().to_arrow()

    # Total: A(1 active) + B(1 old retired + 1 new active) + C(same) + D(1 active) = 6 rows
    assert len(all_rows) == 6

    # Active rows: A, B_new, C_new, D
    active_rows = [
        all_rows.column('external_id')[i].as_py()
        for i in range(len(all_rows))
        if all_rows.column('is_active')[i].as_py()
    ]
    assert sorted(active_rows) == ['A', 'B', 'C', 'D']

    # Retired rows: B_old, C_old — is_active=false and tombstoned_at is not null
    inactive_rows = [i for i in range(len(all_rows)) if not all_rows.column('is_active')[i].as_py()]
    assert len(inactive_rows) == 2
    for idx in inactive_rows:
        assert all_rows.column('tombstoned_at')[idx].as_py() is not None


# ---------------------------------------------------------------------------
# Test 4: tombstone_batch — pg gate routes to SQLAlchemy, events emitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_batch_routes_to_pg_when_gate_pg(
    session_factory: Any,
    lake_settings_pg: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """gate=pg: tombstone_batch calls SQLAlchemy tombstone path, per-row events emitted."""
    log_service, _ = capturing_log_service
    capturing_events = CapturingEventService()
    from src.platform.events.service import EventService

    event_service = EventService(sink=capturing_events)

    svc = AccessArtifactService(
        event_service=event_service,
        log_service=log_service,
        lake_settings=lake_settings_pg,
    )

    async with session_factory() as session:
        app_id = await _make_application_id(session)
        # Insert two artifacts via single-item path
        from src.inventory.access_artifacts.repository import upsert_access_artifact

        a1, _ = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-ts-1',
            payload={},
            ingest_batch_id='batch-001',
            observed_at=datetime.now(UTC),
        )
        a2, _ = await upsert_access_artifact(
            session,
            application_id=app_id,
            artifact_type='sap_role',
            external_id='role-ts-2',
            payload={},
            ingest_batch_id='batch-001',
            observed_at=datetime.now(UTC),
        )
        await session.flush()
        id1, id2 = a1.id, a2.id

        result = await svc.tombstone_batch(
            session,
            [id1, id2],
            observed_at=datetime.now(UTC),
        )
        await session.commit()

    assert result.backend == 'pg'
    assert result.row_count == 2
    assert result.snapshot_id is None

    # Per-row tombstone events emitted
    tombstoned = capturing_events.filter_by_type('inventory.access_artifact.tombstoned')
    assert len(tombstoned) == 2


# ---------------------------------------------------------------------------
# Test 5: tombstone_batch — iceberg gate, partition read-modify-write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tombstone_batch_routes_to_iceberg_when_gate_iceberg(
    session_factory: Any,
    lake_settings_iceberg: LakeSettings,
    artifacts_table_fixture: Catalog,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """gate=iceberg: tombstone_batch marks rows inactive, creates new snapshot."""
    log_service, _ = capturing_log_service
    catalog = artifacts_table_fixture

    capturing_events = CapturingEventService()
    from src.platform.events.service import EventService

    event_service = EventService(sink=capturing_events)

    svc = AccessArtifactService(
        event_service=event_service,
        log_service=log_service,
        lake_settings=lake_settings_iceberg,
        lake_catalog=catalog,
    )

    app_id = uuid.uuid4()
    items = [
        AccessArtifactBatchItem(application_id=app_id, artifact_type='sap_role', external_id='r1', payload={}),
        AccessArtifactBatchItem(application_id=app_id, artifact_type='sap_role', external_id='r2', payload={}),
        AccessArtifactBatchItem(application_id=app_id, artifact_type='sap_role', external_id='r3', payload={}),
    ]

    async with session_factory() as session:
        upsert_result = await svc.upsert_batch(session, items, ingest_batch_id=uuid.uuid4())

    # Get the IDs of r1 and r2 from the Iceberg table
    table = catalog.load_table(('raw', 'access_artifacts'))
    all_rows = table.scan().to_arrow()

    # Extract IDs of r1 and r2 — they are strings in this test table
    target_ids: list[uuid.UUID] = []
    for i in range(len(all_rows)):
        ext_id = all_rows.column('external_id')[i].as_py()
        if ext_id in ('r1', 'r2'):
            raw_id = all_rows.column('id')[i].as_py()
            target_ids.append(uuid.UUID(str(raw_id)))

    assert len(target_ids) == 2

    snapshot_before = upsert_result.snapshot_id

    async with session_factory() as session:
        ts_result = await svc.tombstone_batch(
            session,
            target_ids,
            observed_at=datetime.now(UTC),
        )

    assert ts_result.backend == 'iceberg'
    assert ts_result.row_count == 2
    assert ts_result.snapshot_id is not None
    # A new snapshot was created
    assert ts_result.snapshot_id != snapshot_before

    # Verify state
    table = catalog.load_table(('raw', 'access_artifacts'))
    final_rows = table.scan().to_arrow()
    assert len(final_rows) == 3

    active_count = sum(1 for i in range(len(final_rows)) if final_rows.column('is_active')[i].as_py())
    assert active_count == 1  # only r3 remains active

    inactive = [i for i in range(len(final_rows)) if not final_rows.column('is_active')[i].as_py()]
    assert len(inactive) == 2
    for idx in inactive:
        assert final_rows.column('tombstoned_at')[idx].as_py() is not None

    # No domain events under iceberg gate
    tombstoned_events = capturing_events.filter_by_type('inventory.access_artifact.tombstoned')
    assert len(tombstoned_events) == 0


# ---------------------------------------------------------------------------
# Test 6: cap 10k — raises AccessArtifactBatchTooLargeError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_batch_too_large_raises(
    session_factory: Any,
    lake_settings_pg: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """Batch of 10_001 items raises AccessArtifactBatchTooLargeError, nothing written."""
    log_service, _ = capturing_log_service
    capturing_events = CapturingEventService()
    from src.platform.events.service import EventService

    event_service = EventService(sink=capturing_events)

    svc = AccessArtifactService(
        event_service=event_service,
        log_service=log_service,
        lake_settings=lake_settings_pg,
    )

    items = _make_items(10_001)
    batch_id = uuid.uuid4()

    with patch('src.inventory.access_artifacts.service.repo_upsert_access_artifact') as mock_repo:
        with pytest.raises(AccessArtifactBatchTooLargeError) as exc_info:
            async with session_factory() as session:
                await svc.upsert_batch(session, items, ingest_batch_id=batch_id)

        # No writes of any kind
        mock_repo.assert_not_called()

    assert exc_info.value.count == 10_001
    assert exc_info.value.limit == 10_000
    assert capturing_events.emitted == []


# ---------------------------------------------------------------------------
# Test 7: backend=iceberg + catalog=None → AccessArtifactLakeNotConfiguredError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_batch_iceberg_no_catalog_raises(
    session_factory: Any,
    lake_settings_iceberg: LakeSettings,
) -> None:
    """backend=iceberg + lake_catalog=None → AccessArtifactLakeNotConfiguredError."""
    svc = AccessArtifactService(
        lake_settings=lake_settings_iceberg,
        lake_catalog=None,
    )

    items = _make_items(1)
    with pytest.raises(AccessArtifactLakeNotConfiguredError):
        async with session_factory() as session:
            await svc.upsert_batch(session, items, ingest_batch_id=uuid.uuid4())


# ---------------------------------------------------------------------------
# Test 8: Iceberg failure → AccessArtifactLakeWriteError, no silent fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_batch_iceberg_failure_raises_and_logs(
    session_factory: Any,
    lake_settings_iceberg: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """When Iceberg load_table raises, AccessArtifactLakeWriteError is raised,
    batch_write_failed ERROR log emitted, SQLAlchemy never called as fallback.
    """
    log_service, sink = capturing_log_service

    # Mock catalog that raises on load_table
    mock_catalog = MagicMock()
    mock_catalog.load_table.side_effect = RuntimeError('iceberg exploded')

    svc = AccessArtifactService(
        log_service=log_service,
        lake_settings=lake_settings_iceberg,
        lake_catalog=mock_catalog,
    )

    items = _make_items(2)
    batch_id = uuid.uuid4()

    with patch('src.inventory.access_artifacts.service.repo_upsert_access_artifact') as mock_repo:
        with pytest.raises(AccessArtifactLakeWriteError):
            async with session_factory() as session:
                await svc.upsert_batch(session, items, ingest_batch_id=batch_id)

        # SQLAlchemy NEVER called — no silent fallback
        mock_repo.assert_not_called()

    # At least one batch_write_failed ERROR log emitted
    error_logs = [r for r in sink.records if 'batch_write_failed' in r.message]
    assert len(error_logs) >= 1
    assert error_logs[0].level == LogLevel.ERROR


# ---------------------------------------------------------------------------
# Test 9: snapshot_id is truthy int in iceberg path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_batch_returns_snapshot_id_for_iceberg_path(
    session_factory: Any,
    lake_settings_iceberg: LakeSettings,
    artifacts_table_fixture: Catalog,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """gate=iceberg: BatchUpsertResult.snapshot_id is a truthy int for use in record_lake_write."""
    log_service, _ = capturing_log_service
    catalog = artifacts_table_fixture

    svc = AccessArtifactService(
        log_service=log_service,
        lake_settings=lake_settings_iceberg,
        lake_catalog=catalog,
    )

    app_id = uuid.uuid4()
    items = _make_items(3, app_id=app_id)

    async with session_factory() as session:
        result = await svc.upsert_batch(session, items, ingest_batch_id=uuid.uuid4())

    assert isinstance(result, BatchUpsertResult)
    assert result.backend == 'iceberg'
    assert result.snapshot_id is not None
    assert isinstance(result.snapshot_id, int)
    assert result.snapshot_id > 0

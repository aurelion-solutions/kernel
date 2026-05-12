# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Explicit double-apply idempotency test for SyncApplyService at the Iceberg level.

ARCH_CONTEXT line 292 invariant (verbatim):
    "Apply runs are idempotent: re-running apply on already-applied items is a no-op.
    Idempotency MUST be enforced at the Iceberg level, not just via PG status.
    PG-status-only check is insufficient: if the kernel crashes after committing an
    Iceberg snapshot but before updating delta_item.status, the next run sees 'pending'
    and would append a duplicate row. Required protocol: at apply run start, perform one
    DuckDB batch scan SELECT DISTINCT reconciliation_delta_item_id FROM
    iceberg_scan('normalized.access_facts') WHERE reconciliation_delta_item_id =
    ANY([pending_ids]) to find already-written items; mark those 'applied' in PG without
    re-writing to Iceberg; proceed with lake_writer only for items not found in Iceberg."

Strategy (Option B from TASK.md §11.6 architect finding):
    The service raises SyncApplyAlreadyExecutedError before reaching the Iceberg-level
    preflight when a completed apply run already exists for the same reconciliation_run_id.
    To exercise the Iceberg-level invariant directly, we manually flip the first
    sync_apply_run row to status='failed' between the two svc.apply() calls, bypassing
    the run-level guard and allowing the second apply to reach preflight_recover_already_written.

Fixtures duplicated from test_service.py — Step 15 chose isolation over conftest extraction;
revisit if a third sync_apply test file is added (housekeeping-backlog).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType
import pytest
from sqlalchemy import update
from src.engines.inventory_reconcile.models import ReconciliationDeltaOperation
from src.engines.inventory_sync.models import SyncApplyRun, SyncApplyRunMode, SyncApplyRunStatus
from src.engines.inventory_sync.tests.test_service import (
    _make_log_service,  # noqa: PLC2701
    _make_service,  # noqa: PLC2701
    _seed_delta_items,  # noqa: PLC2701
    _seed_reconciliation_run,  # noqa: PLC2701
)
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSessionFactory

# ---------------------------------------------------------------------------
# Fixtures (duplicated from test_service.py — see module docstring)
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
        except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
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
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
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
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_double_apply_is_no_op_at_iceberg_level(
    session_factory,
    catalog: Any,
    lake_session_factory: LakeSessionFactory,
    capturing_events: CapturingEventService,
    event_service: EventService,
) -> None:
    """Double apply exercises the Iceberg-level idempotency guard (ARCH_CONTEXT line 292).

    ARCH_CONTEXT line 292 invariant (verbatim):
        "Apply runs are idempotent: re-running apply on already-applied items is a no-op.
        Idempotency MUST be enforced at the Iceberg level, not just via PG status."

    Option B strategy (architect TASK.md §11.6):
        Between the two svc.apply() calls, the first sync_apply_run row is manually
        flipped to status='failed', bypassing the run-level guard (SyncApplyAlreadyExecutedError)
        so the second apply reaches preflight_recover_already_written and exercises the
        Iceberg-level scan. This is the scenario where the kernel crashed after committing
        the Iceberg snapshot but before completing the PG status update.

    Assertions:
        (a) No new Iceberg snapshot after second apply (snap_after == snap_before_second).
        (b) resp2.applied_count == 3 (all 3 recovered by preflight).
        (c) 6 total inventory.access_fact.created events (3 from first apply + 3 from
            second apply's recovered path) — per service.py line 542 contract which emits
            events for ALL processed items including recovered ones.
        (d) Iceberg row count for the 3 items == 3, not 6 (no duplicate writes).
    """
    # --- Setup: reconciliation run with 3 approved create items ---
    async with session_factory() as session:
        run = await _seed_reconciliation_run(session)
        items = await _seed_delta_items(
            session,
            run=run,
            count=3,
            operation=ReconciliationDeltaOperation.create,
        )
        run_id = run.id
        item_ids = [item.id for item in items]

        # --- First apply ---
        svc = _make_service(session, lake_session_factory, catalog, event_service)
        resp1 = await svc.apply(
            reconciliation_run_id=run_id,
            mode=SyncApplyRunMode.auto_apply,
        )
        await session.commit()

    assert resp1.applied_count == 3
    assert resp1.failed_count == 0
    assert resp1.status == SyncApplyRunStatus.completed

    # Capture snapshot before second apply
    iceberg_table = catalog.load_table(('normalized', 'access_facts'))
    snap_before_second = iceberg_table.current_snapshot()
    assert snap_before_second is not None, 'First apply must produce an Iceberg snapshot'

    # After first apply: 3 events emitted
    created_after_first = capturing_events.filter_by_type('inventory.access_fact.created')
    assert len(created_after_first) == 3

    # --- Option B: simulate kernel crash after Iceberg commit ---
    # Full crash simulation requires three resets:
    #   1. sync_apply_run.status = 'failed' — bypasses run-level guard
    #      (SyncApplyAlreadyExecutedError checks running|completed|partially_applied).
    #   2. delta_item.status = 'approved', applied_at = NULL — simulates crash BEFORE
    #      mark_delta_items_applied ran (items still show as "pending apply" in PG
    #      even though Iceberg already has them). This is the exact scenario from
    #      ARCH_CONTEXT line 292: "kernel crashes after committing an Iceberg snapshot
    #      but before updating delta_item.status".
    #   3. reconciliation_run.status = 'pending_apply' — so get_reconciliation_run
    #      returns the run (it searches by ID, not status, but update_reconciliation_run_status
    #      also ran and must be undone conceptually).
    from src.engines.inventory_reconcile.models import (  # noqa: PLC0415
        ReconciliationDeltaItem,
        ReconciliationDeltaItemStatus,
        ReconciliationRun,
        ReconciliationRunStatus,
    )

    async with session_factory() as session:
        # 1. Flip apply run to 'failed' — run-level guard now won't block second apply
        await session.execute(
            update(SyncApplyRun)
            .where(SyncApplyRun.reconciliation_run_id == run_id)
            .values(status=SyncApplyRunStatus.failed)
        )
        # 2. Reset delta items to 'approved' — simulate crash before mark_delta_items_applied
        await session.execute(
            update(ReconciliationDeltaItem)
            .where(ReconciliationDeltaItem.reconciliation_run_id == run_id)
            .values(status=ReconciliationDeltaItemStatus.approved, applied_at=None)
        )
        # 3. Reset reconciliation run to 'pending_apply'
        await session.execute(
            update(ReconciliationRun)
            .where(ReconciliationRun.id == run_id)
            .values(status=ReconciliationRunStatus.pending_apply)
        )
        await session.commit()

    # --- Second apply: preflight detects all 3 items already in Iceberg ---
    async with session_factory() as session:
        svc2 = _make_service(session, lake_session_factory, catalog, event_service)
        resp2 = await svc2.apply(
            reconciliation_run_id=run_id,
            mode=SyncApplyRunMode.auto_apply,
        )
        await session.commit()

    # (a) No new Iceberg snapshot: preflight recovered all 3, write_run_batch never called
    iceberg_table_after = catalog.load_table(('normalized', 'access_facts'))
    snap_after = iceberg_table_after.current_snapshot()
    assert snap_after == snap_before_second, (
        'Second apply must not rotate the Iceberg snapshot: all items recovered by preflight'
    )

    # (b) applied_count == 3 (all 3 recovered via preflight path)
    assert resp2.applied_count == 3
    assert resp2.failed_count == 0

    # (c) 6 total inventory.access_fact.created events (3 first + 3 second recovered path)
    # Per service.py line 542: events are emitted for ALL processed items including
    # recovered ones. This is the current contract; do NOT suppress recovered-path events.
    all_created = capturing_events.filter_by_type('inventory.access_fact.created')
    assert len(all_created) == 6

    # (d) Iceberg row count for the 3 items == 3, not 6 (no duplicate writes)
    # Verify via DuckDB scan using lake_session; use iceberg_table_path for the
    # correct warehouse-relative path (strips file:// prefix automatically).
    lake_session = lake_session_factory.acquire()
    table_path = lake_session.iceberg_table_path('normalized', 'access_facts')
    item_ids_str = ', '.join(f"'{iid!s}'" for iid in item_ids)
    row_count_result = lake_session.execute(
        f"SELECT COUNT(*) AS cnt FROM iceberg_scan('{table_path}') "
        f'WHERE reconciliation_delta_item_id IN ({item_ids_str})'
    )
    row_count = row_count_result.fetchone()[0]
    assert row_count == 3, (
        f'Expected exactly 3 Iceberg rows for the 3 items, got {row_count}. '
        'Second apply must not duplicate Iceberg writes.'
    )

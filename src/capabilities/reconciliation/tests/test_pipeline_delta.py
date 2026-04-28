# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end pipeline delta test for Phase 15 Step 8.

Strategy
--------
- Seed ``raw.access_artifacts`` directly via PyIceberg (production schema would
  use UUID partitions; here we use the string-partition fixture to avoid the
  PyArrow 24 ``group_by`` limitation on UUID partition fields).
- Seed ``normalized.access_facts`` directly via PyIceberg (same workaround).
- Bootstrap ``ref_actions_local`` TEMP TABLE manually in the DuckDB session
  (test-only affordance; production uses ``pg_dsn`` ATTACH).
- Run ``run_reconciliation``; assert:
  - 1 ``ReconciliationRun`` with ``status='pending_apply'``
  - delta_items distribution matches seeded diff
  - ``reactivate_count == 0`` (reactivation deferred to Step 12)
  - ``noop`` count == 0 (no noop rows emitted)
  - ``unchanged_count`` matches
  - ``observed_snapshot_id`` and ``current_snapshot_id`` populated
  - ``normalized.access_facts.current_snapshot()`` unchanged after run
    (invariant: zero lake writes)
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
import uuid

import pyarrow as pa
from pyiceberg.catalog import Catalog
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType
import pytest
from src.capabilities.reconciliation.contracts import NormalizationResult
from src.capabilities.reconciliation.models import ReconciliationDeltaOperation, ReconciliationRunStatus
from src.capabilities.reconciliation.pipeline import run_reconciliation
from src.capabilities.reconciliation.registry import _reset_registry_for_tests, register_handler
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSession, LakeSessionFactory
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_registry_fixture():
    _reset_registry_for_tests()
    yield
    _reset_registry_for_tests()


@pytest.fixture(autouse=True)
def reset_catalog():
    reset_catalog_cache_for_tests()
    yield
    reset_catalog_cache_for_tests()


@pytest.fixture
def capturing_log_service() -> tuple[LogService, CapturingLogSink]:
    sink = CapturingLogSink()
    return LogService(sink=sink), sink


@pytest.fixture
def lake_settings_iceberg(tmp_path: Path) -> LakeSettings:
    return LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/catalog.db',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
        artifacts_write_backend='iceberg',
    )


def _make_string_schema_artifacts() -> tuple[Schema, PartitionSpec]:
    """String-only schema for raw.access_artifacts (test-only; all optional for PyArrow compat).

    PyArrow arrays are nullable by default; setting required=False avoids
    the PyIceberg schema-mismatch error when appending.
    """
    schema = Schema(
        NestedField(1, 'id', StringType(), required=False),
        NestedField(2, 'application_id', StringType(), required=False),
        NestedField(3, 'artifact_type', StringType(), required=False),
        NestedField(4, 'external_id', StringType(), required=False),
        NestedField(5, 'payload', StringType(), required=False),
        NestedField(6, 'raw_name', StringType(), required=False),
        NestedField(7, 'effect', StringType(), required=False),
        NestedField(8, 'valid_from', TimestamptzType(), required=False),
        NestedField(9, 'valid_until', TimestamptzType(), required=False),
        NestedField(10, 'is_active', BooleanType(), required=False),
        NestedField(11, 'tombstoned_at', TimestamptzType(), required=False),
        NestedField(12, 'observed_at', TimestamptzType(), required=False),
        NestedField(13, 'ingested_at', TimestamptzType(), required=False),
        NestedField(14, 'ingest_batch_id', StringType(), required=False),
    )
    spec = PartitionSpec(
        PartitionField(source_id=3, field_id=1000, transform=IdentityTransform(), name='artifact_type')
    )
    return schema, spec


def _make_string_schema_facts() -> tuple[Schema, PartitionSpec]:
    """String-only schema for normalized.access_facts (test-only; all optional for PyArrow compat)."""
    schema = Schema(
        NestedField(1, 'id', StringType(), required=False),
        NestedField(2, 'subject_id', StringType(), required=False),
        NestedField(3, 'account_id', StringType(), required=False),
        NestedField(4, 'resource_id', StringType(), required=False),
        NestedField(5, 'action_id', StringType(), required=False),
        NestedField(6, 'effect', StringType(), required=False),
        NestedField(7, 'valid_from', TimestamptzType(), required=False),
        NestedField(8, 'valid_until', TimestamptzType(), required=False),
        NestedField(9, 'is_active', BooleanType(), required=False),
        NestedField(10, 'observed_at', TimestamptzType(), required=False),
        NestedField(11, 'created_at', TimestamptzType(), required=False),
        NestedField(12, 'revoked_at', TimestamptzType(), required=False),
        NestedField(13, 'latest_batch_id', StringType(), required=False),
        NestedField(14, 'application_id_denorm', StringType(), required=False),
        NestedField(15, 'subject_kind_denorm', StringType(), required=False),
        NestedField(16, 'reconciliation_delta_item_id', StringType(), required=False),
        NestedField(17, 'natural_key_hash', StringType(), required=False),
    )
    spec = PartitionSpec(
        PartitionField(source_id=15, field_id=1000, transform=IdentityTransform(), name='subject_kind_denorm')
    )
    return schema, spec


@pytest.fixture
def seeded_lake_catalog(
    lake_settings_iceberg: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> tuple[Catalog, uuid.UUID, list[dict], list[dict]]:
    """Provision test tables and seed data; return (catalog, app_id, artifacts, facts)."""
    log, _ = capturing_log_service
    catalog = get_catalog(lake_settings_iceberg, log_service=log)

    # Create namespaces
    for ns in (('raw',), ('normalized',)):
        try:
            catalog.create_namespace(ns)
        except Exception:
            pass

    app_id = uuid.uuid4()
    now = datetime.now(UTC)

    # --- raw.access_artifacts ---
    art_schema, art_spec = _make_string_schema_artifacts()
    try:
        catalog.drop_table(('raw', 'access_artifacts'))
    except Exception:
        pass
    art_table = catalog.create_table(('raw', 'access_artifacts'), schema=art_schema, partition_spec=art_spec)

    # Seed 10 active artifacts (type='role')
    artifact_rows = []
    for i in range(10):
        artifact_rows.append(
            {
                'id': str(uuid.uuid4()),
                'application_id': str(app_id),
                'artifact_type': 'role',
                'external_id': f'ext-{i}',
                'payload': json.dumps(
                    {
                        'subject_id': str(uuid.uuid4()),
                        'resource_key': f'res-{i}',
                        'resource_type': 'database',
                        'action_slug': 'read',
                        'effect': 'allow',
                    }
                ),
                'raw_name': f'role-{i}',
                'effect': 'allow',
                'valid_from': now,
                'valid_until': None,
                'is_active': True,
                'tombstoned_at': None,
                'observed_at': now,
                'ingested_at': now,
                'ingest_batch_id': None,
            }
        )

    arrow_art = pa.table(
        {
            'id': pa.array([r['id'] for r in artifact_rows], type=pa.string()),
            'application_id': pa.array([r['application_id'] for r in artifact_rows], type=pa.string()),
            'artifact_type': pa.array([r['artifact_type'] for r in artifact_rows], type=pa.string()),
            'external_id': pa.array([r['external_id'] for r in artifact_rows], type=pa.string()),
            'payload': pa.array([r['payload'] for r in artifact_rows], type=pa.string()),
            'raw_name': pa.array([r['raw_name'] for r in artifact_rows], type=pa.string()),
            'effect': pa.array([r['effect'] for r in artifact_rows], type=pa.string()),
            'valid_from': pa.array([r['valid_from'] for r in artifact_rows], type=pa.timestamp('us', tz='UTC')),
            'valid_until': pa.array([None] * len(artifact_rows), type=pa.timestamp('us', tz='UTC')),
            'is_active': pa.array([r['is_active'] for r in artifact_rows], type=pa.bool_()),
            'tombstoned_at': pa.array([None] * len(artifact_rows), type=pa.timestamp('us', tz='UTC')),
            'observed_at': pa.array([r['observed_at'] for r in artifact_rows], type=pa.timestamp('us', tz='UTC')),
            'ingested_at': pa.array([r['ingested_at'] for r in artifact_rows], type=pa.timestamp('us', tz='UTC')),
            'ingest_batch_id': pa.array([None] * len(artifact_rows), type=pa.string()),
        }
    )
    art_table.append(arrow_art)

    # --- normalized.access_facts ---
    fact_schema, fact_spec = _make_string_schema_facts()
    try:
        catalog.drop_table(('normalized', 'access_facts'))
    except Exception:
        pass
    fact_table = catalog.create_table(('normalized', 'access_facts'), schema=fact_schema, partition_spec=fact_spec)

    # Seed 3 active facts (will become "revoked" since artifacts don't match by FactKey)
    fact_rows = []
    for _i in range(3):
        import hashlib

        subj = uuid.uuid4()
        res = uuid.uuid4()
        action_id_int = 1  # corresponds to 'read' slug id
        effect = 'allow'
        hash_raw = f'{app_id}|{subj}|\\x00|{res}|{action_id_int}|{effect}'
        nat_hash = hashlib.sha256(hash_raw.encode()).hexdigest()
        fact_rows.append(
            {
                'id': str(uuid.uuid4()),
                'subject_id': str(subj),
                'account_id': None,
                'resource_id': str(res),
                'action_id': str(action_id_int),
                'effect': effect,
                'valid_from': now,
                'valid_until': None,
                'is_active': True,
                'observed_at': now,
                'created_at': now,
                'revoked_at': None,
                'latest_batch_id': None,
                'application_id_denorm': str(app_id),
                'subject_kind_denorm': 'employee',
                'reconciliation_delta_item_id': None,
                'natural_key_hash': nat_hash,
            }
        )

    arrow_facts = pa.table(
        {
            'id': pa.array([r['id'] for r in fact_rows], type=pa.string()),
            'subject_id': pa.array([r['subject_id'] for r in fact_rows], type=pa.string()),
            'account_id': pa.array([None] * len(fact_rows), type=pa.string()),
            'resource_id': pa.array([r['resource_id'] for r in fact_rows], type=pa.string()),
            'action_id': pa.array([r['action_id'] for r in fact_rows], type=pa.string()),
            'effect': pa.array([r['effect'] for r in fact_rows], type=pa.string()),
            'valid_from': pa.array([r['valid_from'] for r in fact_rows], type=pa.timestamp('us', tz='UTC')),
            'valid_until': pa.array([None] * len(fact_rows), type=pa.timestamp('us', tz='UTC')),
            'is_active': pa.array([r['is_active'] for r in fact_rows], type=pa.bool_()),
            'observed_at': pa.array([r['observed_at'] for r in fact_rows], type=pa.timestamp('us', tz='UTC')),
            'created_at': pa.array([r['created_at'] for r in fact_rows], type=pa.timestamp('us', tz='UTC')),
            'revoked_at': pa.array([None] * len(fact_rows), type=pa.timestamp('us', tz='UTC')),
            'latest_batch_id': pa.array([None] * len(fact_rows), type=pa.string()),
            'application_id_denorm': pa.array([r['application_id_denorm'] for r in fact_rows], type=pa.string()),
            'subject_kind_denorm': pa.array([r['subject_kind_denorm'] for r in fact_rows], type=pa.string()),
            'reconciliation_delta_item_id': pa.array([None] * len(fact_rows), type=pa.string()),
            'natural_key_hash': pa.array([r['natural_key_hash'] for r in fact_rows], type=pa.string()),
        }
    )
    fact_table.append(arrow_facts)

    return catalog, app_id, artifact_rows, fact_rows


@pytest.fixture
def lake_session_with_ref_actions(
    lake_settings_iceberg: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> LakeSession:
    """Return a LakeSession with ref_actions_local TEMP TABLE seeded manually.

    Test-only affordance: production uses pg_dsn ATTACH.
    We pass pg_dsn=None (skips Pattern-3) then create the TEMP TABLE ourselves.
    """
    log, _ = capturing_log_service
    factory = LakeSessionFactory(settings=lake_settings_iceberg, log_service=log, pg_dsn=None)
    session = factory.acquire()

    # Manually create ref_actions_local TEMP TABLE with 'read' action
    session.execute('CREATE TEMP TABLE IF NOT EXISTS ref_actions_local (id BIGINT, slug VARCHAR)')
    session.execute("INSERT INTO ref_actions_local VALUES (1, 'read')")
    session.execute("INSERT INTO ref_actions_local VALUES (2, 'write')")
    session.execute("INSERT INTO ref_actions_local VALUES (3, 'execute')")

    return session


# ---------------------------------------------------------------------------
# Handler fixture
# ---------------------------------------------------------------------------


def _register_passthrough_handler(session_holder: list[Any]) -> None:
    """Register a handler that parses payload JSON and returns NormalizationResult."""

    class _PayloadHandler:
        async def handle(self, artifact: Any, session: Any) -> list[NormalizationResult]:
            try:
                payload = json.loads(artifact.payload) if artifact.payload else {}
            except (json.JSONDecodeError, TypeError):
                return []

            subject_id_str = payload.get('subject_id')
            resource_key = payload.get('resource_key')
            action_slug = payload.get('action_slug', 'read')
            effect = payload.get('effect', 'allow')

            if not subject_id_str or not resource_key:
                return []

            # Resolve resource via ResourceService
            from sqlalchemy import select
            from src.inventory.resources.models import Resource

            resource_type = payload.get('resource_type', 'database')

            # Find or create resource
            result = await session.execute(
                select(Resource).where(
                    Resource.resource_key == resource_key,
                )
            )
            resource = result.scalar_one_or_none()
            if resource is None:
                # Create it
                resource = Resource(
                    external_id=resource_key,
                    application_id=artifact.application_id,
                    kind=resource_type,
                    resource_type=resource_type,
                    resource_key=resource_key,
                )
                session.add(resource)
                await session.flush()

            return [
                NormalizationResult(
                    subject_id=uuid.UUID(subject_id_str),
                    account_id=None,
                    resource_id=resource.id,
                    action_slug=action_slug,
                    effect=effect,
                    valid_from=None,
                    valid_until=None,
                )
            ]

    register_handler('role', _PayloadHandler())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pipeline_delta_creates_run_and_items(
    session_factory: Any,
    seeded_lake_catalog: tuple[Catalog, uuid.UUID, list[dict], list[dict]],
    lake_session_with_ref_actions: LakeSession,
    lake_settings_iceberg: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
):
    """Full pipeline: 10 artifacts → delta items + pending_apply run.

    Artifacts have 'role' payload with new subjects/resources not in current_facts.
    Current facts (3) have completely different keys → all get revoked.
    All artifacts produce new keys → 10 creates.

    Invariant: normalized.access_facts snapshot unchanged after run.
    """
    _register_passthrough_handler([])

    catalog, app_id, artifact_rows, fact_rows = seeded_lake_catalog
    lake_sess = lake_session_with_ref_actions

    # Capture facts snapshot BEFORE the run
    facts_tbl = catalog.load_table(('normalized', 'access_facts'))
    snapshot_before = facts_tbl.current_snapshot()
    snapshot_id_before = snapshot_before.snapshot_id if snapshot_before else None

    # We need an application row in PG for the FK on reconciliation_runs
    async with session_factory() as session:
        from src.platform.applications.models import Application

        pg_app = Application(
            id=app_id,
            name=f'pipeline-test-{app_id}',
            code=f'pt-{str(app_id)[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(pg_app)
        await session.flush()
        await session.commit()

    async with session_factory() as session:
        summary = await run_reconciliation(
            session,
            lake_sess,
            catalog,
            application_id=app_id,
        )
        await session.commit()

    # Verify run status and counts
    assert summary.run_id is not None
    assert summary.application_id == app_id

    # All 10 artifacts produce new keys → 10 creates
    # 3 current facts have unrelated keys → 3 revokes
    assert summary.facts_created == 10
    assert summary.facts_revoked == 3
    assert summary.facts_updated == 0
    # No reactivate in Step 8
    assert summary.artifacts_ingested == 10
    assert summary.facts_errored == 0

    # Snapshot ids captured
    assert summary.observed_snapshot_id is not None
    assert summary.current_snapshot_id is not None

    # Verify ReconciliationRun in DB
    async with session_factory() as session:
        from sqlalchemy import select
        from src.capabilities.reconciliation.models import ReconciliationDeltaItem, ReconciliationRun

        run_result = await session.execute(select(ReconciliationRun).where(ReconciliationRun.id == summary.run_id))
        run = run_result.scalar_one()
        assert run.status == ReconciliationRunStatus.pending_apply
        assert run.created_count == 10
        assert run.revoked_count == 3

        item_result = await session.execute(
            select(ReconciliationDeltaItem).where(ReconciliationDeltaItem.reconciliation_run_id == summary.run_id)
        )
        items = item_result.scalars().all()
        operations = [item.operation for item in items]

    create_count = operations.count(ReconciliationDeltaOperation.create)
    revoke_count = operations.count(ReconciliationDeltaOperation.revoke)
    reactivate_count = operations.count(ReconciliationDeltaOperation.reactivate)
    noop_count = operations.count(ReconciliationDeltaOperation.noop)

    assert create_count == 10
    assert revoke_count == 3
    assert reactivate_count == 0  # reactivation deferred to Step 12
    assert noop_count == 0  # no noop rows emitted

    # Invariant: facts snapshot unchanged (zero lake writes in Step 8)
    facts_tbl_after = catalog.load_table(('normalized', 'access_facts'))
    snapshot_after = facts_tbl_after.current_snapshot()
    snapshot_id_after = snapshot_after.snapshot_id if snapshot_after else None
    assert snapshot_id_before == snapshot_id_after, (
        f'normalized.access_facts was written during Step 8 reconciliation! '
        f'before={snapshot_id_before}, after={snapshot_id_after}'
    )


@pytest.mark.asyncio
async def test_pipeline_delta_unchanged_count(
    session_factory: Any,
    lake_settings_iceberg: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
    lake_session_with_ref_actions: LakeSession,
):
    """When artifact keys exactly match current facts, unchanged_count > 0, no delta items."""
    from src.capabilities.reconciliation.pipeline import _compute_natural_key_hash

    _register_passthrough_handler([])

    log, _ = capturing_log_service
    catalog = get_catalog(lake_settings_iceberg, log_service=log)

    app_id = uuid.uuid4()
    now = datetime.now(UTC)

    # Create namespaces and tables
    for ns in (('raw',), ('normalized',)):
        try:
            catalog.create_namespace(ns)
        except Exception:
            pass

    art_schema, art_spec = _make_string_schema_artifacts()
    try:
        catalog.drop_table(('raw', 'access_artifacts'))
    except Exception:
        pass
    art_table = catalog.create_table(('raw', 'access_artifacts'), schema=art_schema, partition_spec=art_spec)

    fact_schema, fact_spec = _make_string_schema_facts()
    try:
        catalog.drop_table(('normalized', 'access_facts'))
    except Exception:
        pass
    fact_table = catalog.create_table(('normalized', 'access_facts'), schema=fact_schema, partition_spec=fact_spec)

    # Create PG app + resource first (Application must be committed before Resource FK)
    async with session_factory() as session:
        from src.platform.applications.models import Application

        pg_app = Application(
            id=app_id,
            name=f'unch-test-{app_id}',
            code=f'uc-{str(app_id)[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(pg_app)
        await session.flush()
        await session.commit()

    subj_id = uuid.uuid4()
    res_id = uuid.uuid4()

    async with session_factory() as session:
        from src.inventory.resources.models import Resource

        resource = Resource(
            id=res_id,
            external_id='unch-res-key',
            application_id=app_id,
            kind='database',
            resource_type='database',
            resource_key='unch-res-key',
        )
        session.add(resource)
        await session.flush()
        await session.commit()

    action_id_int = 1  # 'read'
    effect = 'allow'

    # Seed 1 artifact that matches the existing fact exactly
    art_payload = json.dumps(
        {
            'subject_id': str(subj_id),
            'resource_key': 'unch-res-key',
            'resource_type': 'database',
            'action_slug': 'read',
            'effect': 'allow',
        }
    )
    arrow_art = pa.table(
        {
            'id': pa.array([str(uuid.uuid4())], type=pa.string()),
            'application_id': pa.array([str(app_id)], type=pa.string()),
            'artifact_type': pa.array(['role'], type=pa.string()),
            'external_id': pa.array(['ext-0'], type=pa.string()),
            'payload': pa.array([art_payload], type=pa.string()),
            'raw_name': pa.array(['role-0'], type=pa.string()),
            'effect': pa.array(['allow'], type=pa.string()),
            'valid_from': pa.array([now], type=pa.timestamp('us', tz='UTC')),
            'valid_until': pa.array([None], type=pa.timestamp('us', tz='UTC')),
            'is_active': pa.array([True], type=pa.bool_()),
            'tombstoned_at': pa.array([None], type=pa.timestamp('us', tz='UTC')),
            'observed_at': pa.array([now], type=pa.timestamp('us', tz='UTC')),
            'ingested_at': pa.array([now], type=pa.timestamp('us', tz='UTC')),
            'ingest_batch_id': pa.array([None], type=pa.string()),
        }
    )
    art_table.append(arrow_art)

    # Seed 1 fact with the SAME key
    nat_hash = _compute_natural_key_hash(app_id, subj_id, None, res_id, action_id_int, effect)
    arrow_facts = pa.table(
        {
            'id': pa.array([str(uuid.uuid4())], type=pa.string()),
            'subject_id': pa.array([str(subj_id)], type=pa.string()),
            'account_id': pa.array([None], type=pa.string()),
            'resource_id': pa.array([str(res_id)], type=pa.string()),
            'action_id': pa.array([str(action_id_int)], type=pa.string()),
            'effect': pa.array([effect], type=pa.string()),
            'valid_from': pa.array([now], type=pa.timestamp('us', tz='UTC')),
            'valid_until': pa.array([None], type=pa.timestamp('us', tz='UTC')),
            'is_active': pa.array([True], type=pa.bool_()),
            'observed_at': pa.array([now], type=pa.timestamp('us', tz='UTC')),
            'created_at': pa.array([now], type=pa.timestamp('us', tz='UTC')),
            'revoked_at': pa.array([None], type=pa.timestamp('us', tz='UTC')),
            'latest_batch_id': pa.array([None], type=pa.string()),
            'application_id_denorm': pa.array([str(app_id)], type=pa.string()),
            'subject_kind_denorm': pa.array(['employee'], type=pa.string()),
            'reconciliation_delta_item_id': pa.array([None], type=pa.string()),
            'natural_key_hash': pa.array([nat_hash], type=pa.string()),
        }
    )
    fact_table.append(arrow_facts)

    lake_sess = lake_session_with_ref_actions

    async with session_factory() as session:
        summary = await run_reconciliation(
            session,
            lake_sess,
            catalog,
            application_id=app_id,
        )
        await session.commit()

    assert summary.unchanged_count == 1
    assert summary.facts_created == 0
    assert summary.facts_revoked == 0
    assert summary.facts_updated == 0

    # No delta items created (write-amplification guard)
    async with session_factory() as session:
        from sqlalchemy import func, select
        from src.capabilities.reconciliation.models import ReconciliationDeltaItem

        count_result = await session.execute(
            select(func.count()).where(ReconciliationDeltaItem.reconciliation_run_id == summary.run_id)
        )
        count = count_result.scalar_one()
    assert count == 0

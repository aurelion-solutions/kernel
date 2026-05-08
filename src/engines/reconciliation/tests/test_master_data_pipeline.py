# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for master data reconciliation pipelines (persons, org_units, employees).

These tests run against a real PG session (via the test session_factory fixture)
and a real Iceberg lake (via lake_settings_iceberg).  The lake is provisioned with
a minimal raw table for each entity type.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import uuid

import pyarrow as pa
import pytest
from src.engines.reconciliation.master_data_pipeline import (
    MasterDataReconciliationResult,
    run_master_data_reconciliation,
    run_persons_reconciliation,
)
from src.engines.reconciliation.models import (
    ReconciliationEntityType,
)
from src.engines.reconciliation.repository import create_run
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSessionFactory
from src.platform.lake.schemas import RAW_PERSONS_SCHEMA, RAW_PERSONS_TABLE
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_us() -> int:
    now = datetime.now(UTC)
    return int(now.timestamp() * 1_000_000)


def _make_persons_arrow(
    items: list[dict[str, Any]],
    pa_schema: Any,
) -> Any:
    now_us = _now_us()
    tz = pa.timestamp('us', tz='UTC')

    ids, external_ids, full_names = [], [], []
    is_actives, tombstoned_ats, source_names = [], [], []
    ingest_batch_ids, observed_ats, ingested_ats = [], [], []

    for item in items:
        ids.append(str(uuid.uuid4()))
        external_ids.append(item['external_id'])
        full_names.append(item['full_name'])
        is_actives.append(item.get('is_active', True))
        tombstoned_ats.append(None)
        source_names.append(None)
        ingest_batch_ids.append(str(uuid.uuid4()))
        observed_ats.append(now_us)
        ingested_ats.append(now_us)

    return pa.table(
        {
            'id': pa.array(ids, type=pa.string()),
            'external_id': pa.array(external_ids, type=pa.string()),
            'full_name': pa.array(full_names, type=pa.string()),
            'is_active': pa.array(is_actives, type=pa.bool_()),
            'tombstoned_at': pa.array(tombstoned_ats, type=tz),
            'source_name': pa.array(source_names, type=pa.string()),
            'ingest_batch_id': pa.array(ingest_batch_ids, type=pa.string()),
            'observed_at': pa.array(observed_ats, type=tz),
            'ingested_at': pa.array(ingested_ats, type=tz),
        },
        schema=pa_schema,
    )


@pytest.fixture
def seeded_persons_lake(lake_settings_iceberg, capturing_log_service):
    """Provision raw.persons with 2 active rows and return (catalog, lake_session)."""
    log, _ = capturing_log_service
    catalog = get_catalog(lake_settings_iceberg, log_service=log)

    for ns in (('raw',),):
        try:
            catalog.create_namespace(ns)
        except Exception:
            pass

    try:
        catalog.drop_table(RAW_PERSONS_TABLE)
    except Exception:
        pass

    tbl = catalog.create_table(RAW_PERSONS_TABLE, schema=RAW_PERSONS_SCHEMA)
    pa_schema = tbl.schema().as_arrow()

    arrow = _make_persons_arrow(
        [
            {'external_id': 'EXT-001', 'full_name': 'Alice'},
            {'external_id': 'EXT-002', 'full_name': 'Bob'},
        ],
        pa_schema,
    )
    tbl.append(arrow)

    factory = LakeSessionFactory(settings=lake_settings_iceberg, log_service=log)
    lake_session = factory.acquire()
    yield catalog, lake_session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persons_pipeline_creates_when_pg_empty(session_factory, seeded_persons_lake):
    """All lake persons should be delta-created when PG persons table is empty."""
    _catalog, lake_session = seeded_persons_lake

    async with session_factory() as session:
        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.person)
        await session.flush()

        result = await run_persons_reconciliation(session, lake_session, run=run)
        await session.commit()

    assert isinstance(result, MasterDataReconciliationResult)
    assert result.entity_type == ReconciliationEntityType.person
    assert result.created_count == 2
    assert result.updated_count == 0
    assert result.revoked_count == 0
    assert result.unchanged_count == 0


@pytest.mark.asyncio
async def test_persons_pipeline_unchanged_when_pg_matches(session_factory, seeded_persons_lake):
    """Persons already in PG with matching full_name → unchanged (no delta items)."""
    from src.inventory.persons.models import Person  # noqa: PLC0415

    _catalog, lake_session = seeded_persons_lake

    async with session_factory() as session:
        # Seed PG
        session.add_all(
            [
                Person(external_id='EXT-001', full_name='Alice'),
                Person(external_id='EXT-002', full_name='Bob'),
            ]
        )
        await session.flush()

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.person)
        await session.flush()
        result = await run_persons_reconciliation(session, lake_session, run=run)
        await session.commit()

    assert result.created_count == 0
    assert result.updated_count == 0
    assert result.unchanged_count == 2


@pytest.mark.asyncio
async def test_persons_pipeline_update_when_name_changed(session_factory, seeded_persons_lake):
    """Person full_name mismatch → update delta item."""
    from src.inventory.persons.models import Person  # noqa: PLC0415

    _catalog, lake_session = seeded_persons_lake

    async with session_factory() as session:
        session.add_all(
            [
                Person(external_id='EXT-001', full_name='Old Alice'),  # lake has 'Alice'
                Person(external_id='EXT-002', full_name='Bob'),
            ]
        )
        await session.flush()

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.person)
        await session.flush()
        result = await run_persons_reconciliation(session, lake_session, run=run)
        await session.commit()

    assert result.updated_count == 1
    assert result.unchanged_count == 1


@pytest.mark.asyncio
async def test_run_master_data_reconciliation_persons(session_factory, seeded_persons_lake):
    """High-level entrypoint: creates ReconciliationRun + returns result."""
    _catalog, lake_session = seeded_persons_lake

    async with session_factory() as session:
        result = await run_master_data_reconciliation(
            session,
            lake_session,
            entity_type=ReconciliationEntityType.person,
        )
        await session.commit()

    assert result.run_id is not None
    assert result.created_count == 2

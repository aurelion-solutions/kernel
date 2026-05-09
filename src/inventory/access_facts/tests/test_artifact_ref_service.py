# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for AccessFactService.get_artifact_ref.

Strategy:
- Seed Iceberg normalized.access_facts with PyArrow (fact + reconciliation_delta_item_id).
- Seed Iceberg raw.access_artifacts with PyArrow (artifact).
- Seed PG reconciliation_delta_items via SQLAlchemy.
- Assert happy path, fact not found, orphaned delta_item (NULL source_artifact_id).
"""

from __future__ import annotations

from datetime import UTC, datetime
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
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_facts.schemas import AccessFactArtifactRefRead
from src.inventory.access_facts.service import AccessFactArtifactRefNotFoundError, AccessFactService
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSessionFactory
from src.platform.logs.service import NoOpLogService

_NOW = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)

_FACTS_PA_SCHEMA = pa.schema(
    [
        pa.field('id', pa.string(), nullable=False),
        pa.field('subject_id', pa.string(), nullable=False),
        pa.field('account_id', pa.string(), nullable=True),
        pa.field('resource_id', pa.string(), nullable=False),
        pa.field('action_id', pa.string(), nullable=False),
        pa.field('effect', pa.string(), nullable=False),
        pa.field('valid_from', pa.timestamp('us', tz='UTC'), nullable=False),
        pa.field('valid_until', pa.timestamp('us', tz='UTC'), nullable=True),
        pa.field('is_active', pa.bool_(), nullable=False),
        pa.field('observed_at', pa.timestamp('us', tz='UTC'), nullable=False),
        pa.field('created_at', pa.timestamp('us', tz='UTC'), nullable=False),
        pa.field('revoked_at', pa.timestamp('us', tz='UTC'), nullable=True),
        pa.field('latest_batch_id', pa.string(), nullable=True),
        pa.field('application_id_denorm', pa.string(), nullable=True),
        pa.field('subject_kind_denorm', pa.string(), nullable=False),
        pa.field('reconciliation_delta_item_id', pa.string(), nullable=False),
        pa.field('natural_key_hash', pa.string(), nullable=False),
    ]
)

_ARTIFACTS_PA_SCHEMA = pa.schema(
    [
        pa.field('id', pa.string(), nullable=False),
        pa.field('application_id', pa.string(), nullable=False),
        pa.field('artifact_type', pa.string(), nullable=False),
        pa.field('external_id', pa.string(), nullable=False),
        pa.field('payload', pa.string(), nullable=True),
        pa.field('raw_name', pa.string(), nullable=True),
        pa.field('effect', pa.string(), nullable=True),
        pa.field('valid_from', pa.timestamp('us', tz='UTC'), nullable=True),
        pa.field('valid_until', pa.timestamp('us', tz='UTC'), nullable=True),
        pa.field('is_active', pa.bool_(), nullable=True),
        pa.field('tombstoned_at', pa.timestamp('us', tz='UTC'), nullable=True),
        pa.field('observed_at', pa.timestamp('us', tz='UTC'), nullable=True),
        pa.field('ingested_at', pa.timestamp('us', tz='UTC'), nullable=True),
        pa.field('ingest_batch_id', pa.string(), nullable=True),
    ]
)


def _iceberg_facts_schema() -> Schema:
    return Schema(
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
        NestedField(14, 'application_id_denorm', StringType(), required=False),
        NestedField(15, 'subject_kind_denorm', StringType(), required=True),
        NestedField(16, 'reconciliation_delta_item_id', StringType(), required=True),
        NestedField(17, 'natural_key_hash', StringType(), required=True),
    )


def _iceberg_artifacts_schema() -> Schema:
    return Schema(
        NestedField(1, 'id', StringType(), required=True),
        NestedField(2, 'application_id', StringType(), required=True),
        NestedField(3, 'artifact_type', StringType(), required=True),
        NestedField(4, 'external_id', StringType(), required=True),
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
def iceberg_catalog(lake_settings: LakeSettings) -> Catalog:
    log = NoOpLogService()
    cat = get_catalog(lake_settings, log_service=log)
    for ns in (('normalized',), ('raw',)):
        try:
            cat.create_namespace(ns)
        except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
            pass
    cat.create_table(
        ('normalized', 'access_facts'),
        schema=_iceberg_facts_schema(),
        partition_spec=PartitionSpec(
            PartitionField(source_id=15, field_id=1000, transform=IdentityTransform(), name='subject_kind_denorm')
        ),
    )
    cat.create_table(
        ('raw', 'access_artifacts'),
        schema=_iceberg_artifacts_schema(),
        partition_spec=PartitionSpec(
            PartitionField(source_id=3, field_id=1000, transform=IdentityTransform(), name='artifact_type')
        ),
    )
    return cat


@pytest.fixture
def lake_session(lake_settings: LakeSettings) -> Any:  # noqa: ANN401
    log = NoOpLogService()
    factory = LakeSessionFactory(settings=lake_settings, log_service=log, pg_dsn=None)
    session = factory.acquire()
    yield session
    session.__exit__(None, None, None)
    factory.close_all()


@pytest_asyncio.fixture
async def pg_session(session_factory: Any) -> Any:  # noqa: ANN401
    async with session_factory() as session:
        yield session
        await session.rollback()


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _seed_iceberg_fact(catalog: Catalog, fact_id: uuid.UUID, delta_item_id: uuid.UUID) -> None:
    table = catalog.load_table(('normalized', 'access_facts'))
    table.append(
        pa.table(
            {
                'id': [str(fact_id)],
                'subject_id': [str(uuid.uuid4())],
                'account_id': [None],
                'resource_id': [str(uuid.uuid4())],
                'action_id': ['read'],
                'effect': ['allow'],
                'valid_from': [_NOW],
                'valid_until': [None],
                'is_active': [True],
                'observed_at': [_NOW],
                'created_at': [_NOW],
                'revoked_at': [None],
                'latest_batch_id': [None],
                'application_id_denorm': [str(uuid.uuid4())],
                'subject_kind_denorm': ['User'],
                'reconciliation_delta_item_id': [str(delta_item_id)],
                'natural_key_hash': ['a' * 64],
            },
            schema=_FACTS_PA_SCHEMA,
        )
    )


def _seed_iceberg_artifact(
    catalog: Catalog, artifact_id: uuid.UUID, application_id: uuid.UUID, external_id: str
) -> None:
    table = catalog.load_table(('raw', 'access_artifacts'))
    table.append(
        pa.table(
            {
                'id': [str(artifact_id)],
                'application_id': [str(application_id)],
                'artifact_type': ['acl_entry'],
                'external_id': [external_id],
                'payload': ['{}'],
                'raw_name': ['test'],
                'effect': ['allow'],
                'valid_from': [_NOW],
                'valid_until': [None],
                'is_active': [True],
                'tombstoned_at': [None],
                'observed_at': [_NOW],
                'ingested_at': [_NOW],
                'ingest_batch_id': [None],
            },
            schema=_ARTIFACTS_PA_SCHEMA,
        )
    )


async def _seed_delta_item(
    pg_session: AsyncSession,
    delta_item_id: uuid.UUID,
    *,
    source_artifact_id: uuid.UUID | None,
) -> None:
    """Insert a minimal ReconciliationDeltaItem row via raw SQL."""
    run_id = uuid.uuid4()
    action_id = (
        await pg_session.execute(sa.text("SELECT id FROM ref_actions WHERE slug = 'read' LIMIT 1"))
    ).scalar_one()

    # Insert a reconciliation_run first (FK constraint)
    await pg_session.execute(
        sa.text(
            """
            INSERT INTO reconciliation_runs (id, application_id, status, created_count,
                                             updated_count, revoked_count, unchanged_count)
            VALUES (:id, NULL, 'running', 0, 0, 0, 0)
            """
        ),
        {'id': run_id},
    )

    await pg_session.execute(
        sa.text(
            """
            INSERT INTO reconciliation_delta_items
              (id, reconciliation_run_id, operation, natural_key_hash,
               subject_id, resource_id, action_id, effect,
               source_artifact_id, status)
            VALUES
              (:id, :run_id, 'create', :natural_key_hash,
               :subject_id, :resource_id, :action_id, 'allow',
               :source_artifact_id, 'pending')
            """
        ),
        {
            'id': delta_item_id,
            'run_id': run_id,
            'natural_key_hash': 'b' * 64,
            'subject_id': str(uuid.uuid4()),
            'resource_id': str(uuid.uuid4()),
            'action_id': action_id,
            'source_artifact_id': str(source_artifact_id) if source_artifact_id else None,
        },
    )
    await pg_session.flush()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_get_artifact_ref_happy_path(
    iceberg_catalog: Catalog,
    lake_session: Any,
    pg_session: AsyncSession,
) -> None:
    """Happy path: fact → delta_item → artifact chain resolves correctly."""
    fact_id = uuid.uuid4()
    delta_item_id = uuid.uuid4()
    artifact_id = uuid.uuid4()
    application_id = uuid.uuid4()
    external_id = 'ext-001'

    _seed_iceberg_fact(iceberg_catalog, fact_id, delta_item_id)
    _seed_iceberg_artifact(iceberg_catalog, artifact_id, application_id, external_id)
    await _seed_delta_item(pg_session, delta_item_id, source_artifact_id=artifact_id)

    service = AccessFactService(log_service=NoOpLogService())
    ref: AccessFactArtifactRefRead = await service.get_artifact_ref(lake_session, pg_session, fact_id)

    assert ref.artifact_id == artifact_id
    assert ref.application_id == application_id
    assert ref.external_id == external_id


async def test_get_artifact_ref_fact_not_found(
    iceberg_catalog: Catalog,
    lake_session: Any,
    pg_session: AsyncSession,
) -> None:
    """Fact not in Iceberg → AccessFactArtifactRefNotFoundError."""
    missing_fact_id = uuid.uuid4()

    service = AccessFactService(log_service=NoOpLogService())
    with pytest.raises(AccessFactArtifactRefNotFoundError) as exc_info:
        await service.get_artifact_ref(lake_session, pg_session, missing_fact_id)

    assert exc_info.value.fact_id == missing_fact_id


async def test_get_artifact_ref_delta_item_orphaned(
    iceberg_catalog: Catalog,
    lake_session: Any,
    pg_session: AsyncSession,
) -> None:
    """Fact exists but source_artifact_id is NULL (orphaned) → AccessFactArtifactRefNotFoundError."""
    fact_id = uuid.uuid4()
    delta_item_id = uuid.uuid4()

    _seed_iceberg_fact(iceberg_catalog, fact_id, delta_item_id)
    await _seed_delta_item(pg_session, delta_item_id, source_artifact_id=None)

    service = AccessFactService(log_service=NoOpLogService())
    with pytest.raises(AccessFactArtifactRefNotFoundError):
        await service.get_artifact_ref(lake_session, pg_session, fact_id)


async def test_get_artifact_ref_artifact_not_in_lake(
    iceberg_catalog: Catalog,
    lake_session: Any,
    pg_session: AsyncSession,
) -> None:
    """Fact + delta_item exist, source_artifact_id is set, but artifact missing from raw.access_artifacts.

    This covers the third broken-link case: step 3 of the chain returns no row.
    """
    fact_id = uuid.uuid4()
    delta_item_id = uuid.uuid4()
    # Use a random artifact_id that is never seeded into Iceberg raw.access_artifacts
    unknown_artifact_id = uuid.uuid4()

    _seed_iceberg_fact(iceberg_catalog, fact_id, delta_item_id)
    # delta_item points to an artifact that doesn't exist in the lake
    await _seed_delta_item(pg_session, delta_item_id, source_artifact_id=unknown_artifact_id)

    service = AccessFactService(log_service=NoOpLogService())
    with pytest.raises(AccessFactArtifactRefNotFoundError) as exc_info:
        await service.get_artifact_ref(lake_session, pg_session, fact_id)

    assert exc_info.value.fact_id == fact_id

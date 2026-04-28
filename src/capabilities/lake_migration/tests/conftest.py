# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Shared fixtures for lake_migration test suite."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSessionFactory
from src.platform.logs.service import NoOpLogService

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog
    from src.platform.lake.duckdb_session import LakeSession

import os
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv('DATABASE_URL', 'postgresql+asyncpg://localhost/aurelion')
parsed = urlparse(DATABASE_URL)
db_name = parsed.path.lstrip('/')
test_db = db_name.rsplit('_', 1)[0] + '_test' if '_' in db_name else db_name + '_test'
TEST_DATABASE_URL = urlunparse(parsed._replace(path='/' + test_db))


@pytest.fixture(autouse=True)
def _reset_catalog() -> None:
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
def catalog(lake_settings: LakeSettings) -> Catalog:
    """Return a bootstrapped catalog with string-based UUID schemas for test compatibility.

    PyArrow 24 does not support ``group_by`` on ``extension<arrow.uuid>`` partition keys,
    which PyIceberg uses for UUID partition fields.  The tests use string-based schemas
    (``StringType`` instead of ``UUIDType``) to avoid this limitation — the same pattern
    used in the kernel ``src/conftest.py`` app fixture.
    """
    from pyiceberg.partitioning import PartitionField, PartitionSpec  # noqa: PLC0415
    from pyiceberg.schema import Schema  # noqa: PLC0415
    from pyiceberg.transforms import IdentityTransform  # noqa: PLC0415
    from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType  # noqa: PLC0415

    cat = get_catalog(lake_settings, log_service=NoOpLogService())

    # Provision namespaces.
    for ns in (('raw',), ('normalized',)):
        try:
            cat.create_namespace(ns)
        except Exception:  # noqa: BLE001
            pass

    # String-based schema for raw.access_artifacts.
    artifact_schema = Schema(
        NestedField(1, 'id', StringType(), required=True),
        NestedField(2, 'application_id', StringType(), required=True),
        NestedField(3, 'artifact_type', StringType(), required=True),
        NestedField(4, 'external_id', StringType(), required=True),
        NestedField(5, 'payload', StringType(), required=False),
        NestedField(6, 'raw_name', StringType(), required=False),
        NestedField(7, 'effect', StringType(), required=False),
        NestedField(8, 'valid_from', TimestamptzType(), required=False),
        NestedField(9, 'valid_until', TimestamptzType(), required=False),
        NestedField(10, 'is_active', BooleanType(), required=True),
        NestedField(11, 'tombstoned_at', TimestamptzType(), required=False),
        NestedField(12, 'observed_at', TimestamptzType(), required=True),
        NestedField(13, 'ingested_at', TimestamptzType(), required=True),
        NestedField(14, 'ingest_batch_id', StringType(), required=False),
    )
    artifact_spec = PartitionSpec(
        PartitionField(source_id=3, field_id=1000, transform=IdentityTransform(), name='artifact_type'),
    )
    cat.create_table(('raw', 'access_artifacts'), schema=artifact_schema, partition_spec=artifact_spec)

    # String-based schema for normalized.access_facts.
    fact_schema = Schema(
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
    fact_spec = PartitionSpec(
        PartitionField(source_id=15, field_id=1000, transform=IdentityTransform(), name='subject_kind_denorm'),
    )
    cat.create_table(('normalized', 'access_facts'), schema=fact_schema, partition_spec=fact_spec)

    return cat


@pytest.fixture
def lake_session(lake_settings: LakeSettings) -> LakeSession:
    factory = LakeSessionFactory(settings=lake_settings, log_service=NoOpLogService(), pg_dsn=None)
    session = factory.acquire()
    yield session
    factory.release(session)


@pytest_asyncio.fixture
async def engine():
    import src.capabilities.lake_migration.models  # noqa: F401
    import src.capabilities.reconciliation.models  # noqa: F401
    import src.capabilities.sync_apply.models  # noqa: F401
    from src.core.db.base import Base  # noqa: PLC0415
    import src.inventory.access_artifacts.models  # noqa: F401
    import src.inventory.access_facts.models  # noqa: F401
    import src.inventory.actions.models  # noqa: F401
    import src.inventory.employees.models  # noqa: F401
    import src.inventory.nhi.models  # noqa: F401
    import src.inventory.persons.models  # noqa: F401
    import src.inventory.resources.models  # noqa: F401
    import src.inventory.subjects.models  # noqa: F401

    eng = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await conn.execute(sa.text("CREATE TYPE llm_provider AS ENUM ('llama_cpp', 'openai', 'ollama')"))
        await conn.run_sync(Base.metadata.create_all)

    from src.inventory.actions.models import Action  # noqa: PLC0415

    async with AsyncSession(eng) as session:
        for slug in ['read', 'write', 'admin', 'use']:
            existing = await session.execute(sa.select(Action.id).where(Action.slug == slug))
            if existing.scalar_one_or_none() is None:
                session.add(Action(slug=slug, description=slug))
        await session.commit()

    try:
        yield eng
    finally:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await eng.dispose()


@pytest.fixture
def session_factory(engine):
    """Return an async_sessionmaker bound to the test engine."""
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )


@pytest_asyncio.fixture
async def db_session(session_factory):
    async with session_factory() as session:
        yield session
        await session.rollback()

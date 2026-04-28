# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from collections.abc import Iterator
import os
import tempfile
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
import src.capabilities.access_analysis.capabilities.models  # noqa: F401 — registers Capability for create_all
import src.capabilities.access_analysis.capability_grants.models  # noqa: F401 — registers CapabilityGrant for create_all
import src.capabilities.access_analysis.capability_mappings.models  # noqa: F401 — registers CapabilityMapping for create_all
import src.capabilities.access_analysis.capability_scope_keys.models  # noqa: F401 — registers CapabilityScopeKey for create_all
import src.capabilities.access_analysis.evaluators.sod  # noqa: F401 — keeps evaluator module discoverable for test collection
import src.capabilities.access_analysis.feedbacks.models  # noqa: F401 — registers Feedback for create_all
import src.capabilities.access_analysis.findings.models  # noqa: F401 — registers Finding for create_all
import src.capabilities.access_analysis.mitigation_controls.models  # noqa: F401 — registers MitigationControl for create_all
import src.capabilities.access_analysis.mitigations.models  # noqa: F401 — registers Mitigation for create_all
import src.capabilities.access_analysis.scan_runs.models  # noqa: F401 — registers ScanRun for create_all
import src.capabilities.access_analysis.sod_rule_conditions.models  # noqa: F401 — registers SodRuleCondition + M2M for create_all
import src.capabilities.access_analysis.sod_rules.models  # noqa: F401 — registers SodRule for create_all
import src.capabilities.effective_access.models  # noqa: F401 — registers EffectiveGrant + partition DDL listeners
import src.capabilities.lake_migration.models  # noqa: F401 — registers LakeMigrationRun for create_all
import src.capabilities.reconciliation.models  # noqa: F401 — registers ReconciliationRun + ReconciliationDeltaItem for create_all
import src.capabilities.sync_apply.models  # noqa: F401 — registers SyncApplyRun + SyncApplyResult for create_all
from src.core.db.base import Base
from src.core.db.deps import get_db
import src.inventory.actions.models  # noqa: F401 — registers ref_actions for create_all
from src.platform.events.buffer import InMemoryEventBuffer
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSessionFactory
import src.platform.llm.models  # noqa: F401 — registers LLMModel for create_all
import src.platform.logs.models  # noqa: F401 — log_event_buffer metadata for create_all
from src.platform.logs.service import NoOpLogService
import src.platform.runtime_settings.models  # noqa: F401 — registers RuntimeSetting for create_all
from src.routers.v0 import router

load_dotenv()


@pytest.fixture(autouse=True)
def _clear_engine_cache() -> Iterator[None]:
    """Clear lru_cache on get_engine and get_session_factory before and after every test.

    Required because AsyncEngine is bound to the asyncio event loop, and
    pytest-asyncio creates a fresh event loop per test function.  Without this
    fixture, the second test that calls get_engine() directly would receive a
    stale engine bound to a dead loop.
    """
    from src.core.db.session import get_engine, get_session_factory  # noqa: PLC0415

    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture(autouse=True, scope='session')
def _default_events_provider_noop() -> Iterator[None]:
    prev = os.environ.get('AURELION_EVENTS_PROVIDER')
    os.environ['AURELION_EVENTS_PROVIDER'] = 'noop'
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop('AURELION_EVENTS_PROVIDER', None)
        else:
            os.environ['AURELION_EVENTS_PROVIDER'] = prev


def _get_database_url() -> str:
    """Resolve the database URL from secrets or fallback to DATABASE_URL env var."""
    # Try new secrets-driven path first
    try:
        from src.core.config import get_settings  # noqa: PLC0415

        return get_settings().postgres.dsn
    except Exception:
        pass
    # Legacy fallback: DATABASE_URL env var (kept for CI environments not yet migrated)
    url = os.getenv('DATABASE_URL')
    if url:
        return url
    raise RuntimeError('Cannot resolve database URL: no secrets file and DATABASE_URL not set')


DATABASE_URL = _get_database_url()
parsed = urlparse(DATABASE_URL)
db_name = parsed.path.lstrip('/')
test_db = db_name.rsplit('_', 1)[0] + '_test' if '_' in db_name else db_name + '_test'
TEST_DATABASE_URL = urlunparse(parsed._replace(path='/' + test_db))


_REF_ACTIONS_SEED = [
    {'slug': 'read', 'description': 'Read access'},
    {'slug': 'write', 'description': 'Write access'},
    {'slug': 'execute', 'description': 'Execute access'},
    {'slug': 'administer', 'description': 'Administer access'},
    {'slug': 'approve', 'description': 'Approve access'},
    {'slug': 'delegate', 'description': 'Delegate access'},
    {'slug': 'review', 'description': 'Review access'},
    # Phase 12 canonical vocabulary (from migration 2026_04_24_0000_add_ref_actions.py)
    {'slug': 'admin', 'description': 'Administer configuration of a resource.'},
    {'slug': 'use', 'description': 'Consume a resource as a functional user.'},
    {'slug': 'own', 'description': 'Ownership-level control of a resource.'},
]


@pytest_asyncio.fixture
async def engine():
    import sqlalchemy as _sa
    from src.inventory.actions.models import Action as _RefAction

    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)

    async with engine.begin() as conn:
        await conn.run_sync(lambda conn: Base.metadata.drop_all(conn, checkfirst=True))
        # llm_provider PG enum is owned by the migration; create explicitly for tests.
        await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await conn.execute(sa.text("CREATE TYPE llm_provider AS ENUM ('llama_cpp', 'openai', 'ollama')"))
        await conn.run_sync(Base.metadata.create_all)

    # Seed reference data
    async with AsyncSession(engine) as session:
        for row in _REF_ACTIONS_SEED:
            existing = await session.execute(_sa.select(_RefAction.id).where(_RefAction.slug == row['slug']))
            if existing.scalar_one_or_none() is None:
                session.add(_RefAction(slug=row['slug'], description=row['description']))
        await session.commit()

    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(lambda conn: Base.metadata.drop_all(conn, checkfirst=True))
            await conn.execute(sa.text('DROP TYPE IF EXISTS llm_provider CASCADE'))
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )


@pytest_asyncio.fixture
async def app(engine):
    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    # Build a minimal in-memory DuckDB lake factory for test routes that
    # now require a LakeSession (e.g. POST /scan-runs/{id}/run).
    # We provision a real (empty) Iceberg warehouse so iceberg_scan returns 0
    # rows instead of raising — the scan completes with 0 unused findings.
    _tmp_dir = tempfile.mkdtemp(prefix='aurelion_test_lake_')
    _test_lake_settings = LakeSettings(
        catalog_url=f'sqlite:///{_tmp_dir}/catalog.db',
        warehouse_uri=f'file://{_tmp_dir}/warehouse',
        storage_provider='file',
    )
    # Provision normalized.access_facts with string-based schema for route tests.
    # String-based UUID fields (StringType) allow route tests to write rows with
    # plain UUIDs without hitting PyArrow 24 extension type limitations.
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.transforms import IdentityTransform
    from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType
    from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests

    reset_catalog_cache_for_tests()
    _test_catalog = get_catalog(_test_lake_settings, log_service=NoOpLogService())

    _test_str_schema = Schema(
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
    _test_str_spec = PartitionSpec(
        PartitionField(source_id=15, field_id=1000, transform=IdentityTransform(), name='subject_kind_denorm')
    )
    try:
        _test_catalog.create_namespace(('normalized',))
    except Exception:
        pass
    try:
        _test_catalog.create_namespace(('raw',))
    except Exception:
        pass
    _test_catalog.create_table(('normalized', 'access_facts'), schema=_test_str_schema, partition_spec=_test_str_spec)
    reset_catalog_cache_for_tests()

    _test_lake_factory = LakeSessionFactory(
        settings=_test_lake_settings,
        log_service=NoOpLogService(),
        pg_dsn=None,
    )

    async def override_get_lake_session():
        session = _test_lake_factory.acquire()
        try:
            yield session
        finally:
            session.__exit__(None, None, None)

    app = FastAPI()
    app.include_router(router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_lake_session] = override_get_lake_session
    app.state.log_service = NoOpLogService()
    app.state.event_buffer = InMemoryEventBuffer()
    app.state.lake_session_factory = _test_lake_factory
    # lake_catalog is read by get_lake_catalog() dependency in lake_migration routes.
    app.state.lake_catalog = _test_catalog
    # Expose catalog for route tests that need to seed Iceberg data
    app.state.test_iceberg_catalog = _test_catalog
    app.state.test_lake_settings = _test_lake_settings
    return app


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as ac:
        yield ac

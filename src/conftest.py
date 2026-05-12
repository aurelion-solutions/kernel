# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Test fixtures for the aurelion-kernel suite.

Architecture
------------
The previous design recreated the full schema (~80 tables, partitions, enum
types) on EVERY test function via ``Base.metadata.drop_all`` /
``create_all``.  That approach was the root cause of three cascade failure
clusters:

  1. ``UniqueViolationError: duplicate key value violates unique constraint
     "pg_type_typname_nsp_index"`` — partial DDL state from a previous test
     left enum types behind that the next ``create_all`` then re-created.
  2. ``UndefinedTableError: relation "ref_actions" does not exist`` — when
     a Postgres backend transiently rolled back a CREATE, dependent tables
     (especially the seed-required ``ref_actions``) appeared to be missing
     for downstream tests.
  3. ``DeadlockDetectedError`` — concurrent DDL paths inside the same test
     fighting for locks on shared catalog rows (``pg_class``, ``pg_type``).

The current design splits responsibilities:

  * ``_provision_test_database`` (session-scoped, autouse) — runs ONCE per
    pytest session.  Synchronously drops all owned objects (ORM tables,
    raw shim tables, owned enum types) and recreates them via
    ``Base.metadata.create_all`` plus the ``llm_provider`` /
    ``access_fact_effect`` enums and the ``access_artifacts`` /
    ``access_facts`` shim tables that some tests still reference.

  * ``engine`` (function-scoped) — yields a fresh ``AsyncEngine`` bound to
    the per-test event loop.  Before yielding it ``TRUNCATE``-s every ORM
    table plus the two shim tables ``RESTART IDENTITY CASCADE`` and
    re-seeds ``ref_actions``.  No DDL runs per-test.

This eliminates all three cascade clusters: there is no per-test DDL, no
opportunity for partial enum state, and no concurrent catalog lock
contention.  A side benefit is roughly two-orders-of-magnitude speedup.

Alembic migration tests (e.g. ``src/engines/reconciliation/tests/
test_migration_delta_model.py``, ``src/engines/sync_apply/tests/
test_migration.py``) bypass these fixtures and drive ``alembic
upgrade``/``downgrade`` against the same test database.  They were the
historical source of cluster (1) and (2): a migration leaves the DB in a
state ``create_all`` cannot reconcile.  The session-scoped provisioner
now runs AFTER any such migration test would have completed (both have
function scope, the provisioner runs first), so the resulting schema is
canonical at the start of each session and any drift introduced mid-session
is reset by the alembic test's own teardown rather than the unit-test path.
"""

from collections.abc import AsyncIterator, Iterator
import os
import tempfile
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from src.core.db.base import Base
from src.core.db.deps import get_db
import src.engines.effective_access.models  # noqa: F401 — registers EffectiveGrant + partition DDL listeners
import src.engines.policy_assessment.policy_types.sod.evaluator  # noqa: F401 — keeps evaluator module discoverable for test collection
import src.engines.reconciliation.models  # noqa: F401 — registers ReconciliationRun + ReconciliationDeltaItem for create_all
import src.engines.sync_apply.models  # noqa: F401 — registers SyncApplyRun + SyncApplyResult for create_all
import src.inventory.access_model.capabilities.models  # noqa: F401 — registers Capability for create_all
import src.inventory.access_model.capability_grants.models  # noqa: F401 — registers CapabilityGrant for create_all
import src.inventory.access_model.capability_mappings.models  # noqa: F401 — registers CapabilityMapping for create_all
import src.inventory.access_model.capability_scope_keys.models  # noqa: F401 — registers CapabilityScopeKey for create_all
import src.inventory.actions.models  # noqa: F401 — registers ref_actions for create_all
import src.inventory.assessment.feedbacks.models  # noqa: F401 — registers Feedback for create_all
import src.inventory.assessment.findings.models  # noqa: F401 — registers Finding for create_all
import src.inventory.assessment.mitigation_controls.models  # noqa: F401 — registers MitigationControl for create_all
import src.inventory.assessment.mitigations.models  # noqa: F401 — registers Mitigation for create_all
import src.inventory.assessment.scan_runs.models  # noqa: F401 — registers ScanRun for create_all
import src.inventory.nhi.models  # noqa: F401 — registers NHI for create_all
import src.inventory.org_units.models  # noqa: F401 — registers OrgUnit for create_all
import src.inventory.policy.sod_rule_conditions.models  # noqa: F401 — registers SodRuleCondition + M2M for create_all
import src.inventory.policy.sod_rules.models  # noqa: F401 — registers SodRule for create_all
import src.inventory.subjects.models  # noqa: F401 — registers Subject for create_all
from src.platform.events.buffer import InMemoryEventBuffer
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSessionFactory
import src.platform.llm.models  # noqa: F401 — registers LLMModel for create_all
import src.platform.logs.models  # noqa: F401 — log_event_buffer metadata for create_all
from src.platform.logs.service import NoOpLogService
import src.platform.orchestrator.models  # noqa: F401 — registers PipelineRun + StepRun + PipelineEventWaiter for create_all
import src.platform.runtime_settings.models  # noqa: F401 — registers RuntimeSetting for create_all
from src.platform.secrets.factory import register_default_providers
from src.routers.v0 import router

load_dotenv()
register_default_providers()


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
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
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


# ---------------------------------------------------------------------------
# Reference data seed
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Raw shim DDL for legacy tables that were dropped in Phase 15 Step 16
# (parity / ACL-bound tests still INSERT into them).
# ---------------------------------------------------------------------------

_ACCESS_ARTIFACTS_DDL = (
    'CREATE TABLE access_artifacts ('
    ' id UUID PRIMARY KEY,'
    ' application_id UUID NOT NULL REFERENCES applications(id) ON DELETE RESTRICT,'
    ' artifact_type VARCHAR(255) NOT NULL,'
    ' external_id VARCHAR(255) NOT NULL,'
    ' payload JSONB NOT NULL,'
    ' ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),'
    ' ingest_batch_id VARCHAR(255),'
    ' observed_at TIMESTAMPTZ NOT NULL,'
    ' raw_name VARCHAR(255),'
    ' effect TEXT,'
    ' valid_from TIMESTAMPTZ,'
    ' valid_until TIMESTAMPTZ,'
    ' is_active BOOLEAN NOT NULL DEFAULT TRUE,'
    ' tombstoned_at TIMESTAMPTZ'
    ')'
)

_ACCESS_FACTS_DDL = (
    'CREATE TABLE access_facts ('
    ' id UUID PRIMARY KEY,'
    ' subject_id UUID NOT NULL REFERENCES subjects(id) ON DELETE RESTRICT,'
    ' account_id UUID,'
    ' resource_id UUID NOT NULL REFERENCES resources(id) ON DELETE RESTRICT,'
    ' action_id BIGINT NOT NULL REFERENCES ref_actions(id) ON DELETE RESTRICT,'
    ' effect access_fact_effect NOT NULL,'
    ' valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),'
    ' valid_until TIMESTAMPTZ,'
    ' is_active BOOLEAN NOT NULL DEFAULT TRUE,'
    ' revoked_at TIMESTAMPTZ,'
    ' observed_at TIMESTAMPTZ NOT NULL,'
    ' created_at TIMESTAMPTZ NOT NULL DEFAULT now()'
    ')'
)


# ---------------------------------------------------------------------------
# Schema setup helpers
# ---------------------------------------------------------------------------


def _shim_table_names() -> tuple[str, ...]:
    """Names of raw-SQL shim tables (not in Base.metadata)."""
    return ('access_facts', 'access_artifacts')


def _truncatable_table_names() -> list[str]:
    """All table names to TRUNCATE between tests, in dependency order.

    Includes the two shim tables that are NOT in ``Base.metadata`` plus every
    ORM table.  ``CASCADE`` handles FK relationships so explicit ordering is
    not required, but we keep shims first for readability.
    """
    names = list(_shim_table_names())
    # Filter out alembic_version: tests must not truncate alembic state because
    # the alembic migration tests rely on it.  The alembic table is also not in
    # Base.metadata (it lives outside the ORM), so it would not appear here
    # anyway, but keep the filter to make the contract explicit.
    names.extend(t.name for t in Base.metadata.sorted_tables if t.name != 'alembic_version')
    return names


async def _drop_everything(conn: sa.Connection) -> None:
    """Wipe every owned object from the test database.

    Order:
      1. Drop shim tables (FK out to ORM tables).
      2. ``Base.metadata.drop_all(checkfirst=True)``.
      3. Drop residual orphan partitions (``effective_grants_*`` may survive
         if a previous migration test left them).
      4. Drop the ``alembic_version`` table — migration tests may have
         populated it; we want a clean slate.
      5. Drop EVERY user-defined enum type in ``public`` schema (catches
         migration-created types that are normally owned by Alembic but
         linger after a migration test).
    """
    # 1. Drop shim tables first (FK dependents of ORM tables).
    for shim in _shim_table_names():
        await conn.execute(sa.text(f'DROP TABLE IF EXISTS {shim} CASCADE'))

    # 1b. Drop retired tables that are no longer in Base.metadata but may still
    #     exist in test databases that were created before Phase 17 Step 13
    #     (lake_migration_runs depends on lake_batches which is in ORM).
    await conn.execute(sa.text('DROP TABLE IF EXISTS lake_migration_runs CASCADE'))

    # 2. Drop ORM-managed objects.
    await conn.run_sync(lambda sync_conn: Base.metadata.drop_all(sync_conn, checkfirst=True))

    # 3. Drop residual partition tables that may leak past drop_all when
    #    SQLAlchemy did not register them as part of metadata.  effective_grants
    #    sub-partitions are CASCADE-dropped above, but a migration test that
    #    aborted mid-upgrade may have left orphan partition siblings.  The IF
    #    EXISTS guard covers a clean DB.
    await conn.execute(sa.text('DROP TABLE IF EXISTS alembic_version CASCADE'))

    # 4. Drop every user-defined enum type that survived above.
    # ``DROP TABLE ... CASCADE`` removes types declared inline as column
    # types when ``create_type=True``; types created out-of-band via
    # ``CREATE TYPE`` (Alembic migrations, the ``effective_grant_effect``
    # before-create listener, etc.) require an explicit drop.
    await conn.execute(
        sa.text(
            'DO $$ DECLARE r record; '
            'BEGIN FOR r IN ('
            '  SELECT n.nspname, t.typname'
            '    FROM pg_type t'
            '    JOIN pg_namespace n ON n.oid = t.typnamespace'
            "   WHERE t.typtype = 'e'"
            "     AND n.nspname = 'public'"
            ") LOOP EXECUTE format('DROP TYPE IF EXISTS %I.%I CASCADE', r.nspname, r.typname); END LOOP; END $$"
        )
    )


async def _create_everything(conn: sa.Connection) -> None:
    """Recreate the canonical test schema.

    Sequence matches the pre-refactor conftest behaviour:
      1. Create the two enums whose models declare ``create_type=False``
         (``llm_provider``, ``access_fact_effect``).
      2. ``Base.metadata.create_all`` — creates all ORM tables and enum
         types whose models declare ``create_type=True``.
      3. Create the two shim tables (``access_artifacts``, ``access_facts``)
         that legacy tests still reference.
    """
    await conn.execute(sa.text("CREATE TYPE llm_provider AS ENUM ('llama_cpp', 'openai', 'ollama')"))
    await conn.execute(sa.text("CREATE TYPE access_fact_effect AS ENUM ('allow', 'deny')"))
    await conn.run_sync(Base.metadata.create_all)
    await conn.execute(sa.text(_ACCESS_ARTIFACTS_DDL))
    await conn.execute(sa.text(_ACCESS_FACTS_DDL))


async def _seed_ref_actions(engine: AsyncEngine) -> None:
    """Idempotently insert the ref_actions seed rows."""
    from src.inventory.actions.models import Action as RefAction  # noqa: PLC0415

    async with AsyncSession(engine) as session:
        for row in _REF_ACTIONS_SEED:
            existing = await session.execute(sa.select(RefAction.id).where(RefAction.slug == row['slug']))
            if existing.scalar_one_or_none() is None:
                session.add(RefAction(slug=row['slug'], description=row['description']))
        await session.commit()


# ---------------------------------------------------------------------------
# Session-scoped schema provisioner
# ---------------------------------------------------------------------------


@pytest.fixture(scope='session', autouse=True)
def _provision_test_database() -> Iterator[None]:
    """Create the canonical test schema once per pytest session.

    Runs in its OWN event loop (``asyncio.run``) so that the AsyncEngine it
    creates is disposed before any test acquires its own loop-bound engine.
    """
    import asyncio  # noqa: PLC0415

    async def _setup() -> None:
        engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await _drop_everything(conn)
                await _create_everything(conn)
            await _seed_ref_actions(engine)
        finally:
            await engine.dispose()

    asyncio.run(_setup())
    yield
    # Intentional: leave schema in place at session end.  A subsequent run will
    # nuke it via _drop_everything anyway, and leaving artefacts simplifies
    # post-mortem debugging when a session crashed mid-flight.


# ---------------------------------------------------------------------------
# Function-scoped engine + per-test data reset
# ---------------------------------------------------------------------------


async def _truncate_all(engine: AsyncEngine) -> None:
    """TRUNCATE every test-owned table and reset identity sequences.

    Single ``TRUNCATE TABLE a, b, c, ... RESTART IDENTITY CASCADE`` statement.
    PG holds an ACCESS EXCLUSIVE lock for the duration but issues no DDL on
    the catalogue, so concurrent test runs would not deadlock on
    ``pg_class``/``pg_type`` the way ``DROP``/``CREATE`` did.
    """
    names = _truncatable_table_names()
    if not names:
        return
    quoted = ', '.join(f'"{n}"' for n in names)
    stmt = f'TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE'
    async with engine.begin() as conn:
        await conn.execute(sa.text(stmt))


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """Function-scoped AsyncEngine bound to the per-test event loop.

    The schema is assumed to exist (provisioned by ``_provision_test_database``).
    Before the test runs, every owned table is truncated and ``ref_actions``
    is re-seeded.  No DDL is issued per-test, which eliminates the duplicate
    enum / undefined table / catalogue-deadlock cascades.
    """
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
    # Defensive: an alembic migration test running just before this one may have
    # invalidated the schema (e.g. ran a downgrade that dropped a table).
    # Re-bootstrap the schema if any required ORM table is missing.  Cheap when
    # everything is fine (single SELECT).
    await _ensure_schema_intact(engine)
    await _truncate_all(engine)
    await _seed_ref_actions(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _ensure_schema_intact(engine: AsyncEngine) -> None:
    """Verify the canonical schema is still present; rebuild it if not.

    Several round-trip migration tests
    (``src/engines/reconciliation/tests/test_migration_delta_model.py``,
    ``src/engines/sync_apply/tests/test_migration.py``)
    leave the database in an arbitrary state — they invoke ``drop_all`` and
    rebuild the schema themselves, and the shim tables this conftest creates
    are not in their working set.  Probe several distinctive objects; if any
    are missing, rebuild from scratch.
    """
    sentinel_tables = ('ref_actions', 'access_facts', 'access_artifacts')
    async with engine.connect() as conn:
        result = await conn.execute(
            sa.text(
                'SELECT table_name FROM information_schema.tables '
                "WHERE table_schema = 'public' AND table_name = ANY(:names)"
            ),
            {'names': list(sentinel_tables)},
        )
        present = {row[0] for row in result.all()}
    if present == set(sentinel_tables):
        return

    async with engine.begin() as conn:
        await _drop_everything(conn)
        await _create_everything(conn)


@pytest_asyncio.fixture
async def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )


@pytest_asyncio.fixture
async def app(engine: AsyncEngine) -> FastAPI:
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
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
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
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass
    try:
        _test_catalog.create_namespace(('raw',))
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
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
    # lake_catalog is read by get_lake_catalog() dependency in sync_apply / reconciliation / platform.lake routes.
    app.state.lake_catalog = _test_catalog
    # Expose catalog for route tests that need to seed Iceberg data
    app.state.test_iceberg_catalog = _test_catalog
    app.state.test_lake_settings = _test_lake_settings
    return app


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as ac:
        yield ac

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Local fixtures for lake pipeline integration tests.

Provides:
- ``lake_settings_iceberg``   — SQLite-backed LakeSettings with both tables provisioned
- ``app_iceberg``             — FastAPI app with Iceberg DI overridden
- ``client_iceberg``          — AsyncClient backed by app_iceberg
- ``seeded_inventory``    — async helper that inserts Application + Subjects + Accounts + Resources
- ``event_history``       — helper that extracts emitted events from InMemoryEventBuffer
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.events.buffer import InMemoryEventBuffer
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_session
from src.platform.lake.duckdb_session import LakeSessionFactory
from src.platform.logs.service import NoOpLogService
from src.routers.v0 import router

# ---------------------------------------------------------------------------
# Fixture path helpers
# ---------------------------------------------------------------------------

_FIXTURES_DIR = Path(__file__).parent / 'fixtures'


def load_pipeline_dataset() -> dict[str, Any]:
    """Load the pipeline_dataset.json fixture."""
    return json.loads((_FIXTURES_DIR / 'pipeline_dataset.json').read_text())


# ---------------------------------------------------------------------------
# Lake setup fixture (string-schema, sqlite catalog, tmp warehouse)
# ---------------------------------------------------------------------------


@pytest.fixture
def lake_settings_iceberg(tmp_path: Path) -> LakeSettings:
    """LakeSettings backed by an in-process SQLite catalog, tmp warehouse."""
    return LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/lake_catalog.db',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
        artifacts_write_backend='iceberg',
    )


# ---------------------------------------------------------------------------
# FastAPI app with lake DI overridden
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def app_iceberg(engine: Any, lake_settings_iceberg: LakeSettings) -> AsyncGenerator[FastAPI]:
    """FastAPI app with Iceberg lake wired for lake pipeline integration tests.

    Uses the same ``engine`` fixture from root conftest for PG sessions.
    Creates a fresh Iceberg catalog with both lake tables provisioned.
    Uses the string-based schema (StringType UUIDs) for compatibility with
    PyArrow 24 (no group_by on extension<arrow.uuid>).
    """
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.transforms import IdentityTransform
    from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from src.core.db.deps import get_db

    # S2: restore production handler registry (defensive — no-op when already correct)
    from src.engines.reconciliation.handlers.acl_entry import AclEntryHandler  # noqa: PLC0415
    from src.engines.reconciliation.handlers.db_grant import DbGrantHandler  # noqa: PLC0415
    from src.engines.reconciliation.handlers.privilege import PrivilegeHandler  # noqa: PLC0415
    from src.engines.reconciliation.handlers.role import RoleHandler  # noqa: PLC0415
    from src.engines.reconciliation.handlers.sap_role import SapRoleHandler  # noqa: PLC0415
    from src.engines.reconciliation.registry import _reset_registry_for_tests, register_handler  # noqa: PLC0415

    _reset_registry_for_tests()
    register_handler('role', RoleHandler())
    register_handler('acl_entry', AclEntryHandler())
    register_handler('privilege', PrivilegeHandler())
    register_handler('db_grant', DbGrantHandler())
    register_handler('sap_role', SapRoleHandler())

    # PG session factory (from root conftest engine)
    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )

    async def override_get_db() -> AsyncGenerator[AsyncSession]:
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
                await session.rollback()
                raise

    # Bootstrap Iceberg catalog with string-schema tables
    reset_catalog_cache_for_tests()
    _catalog = get_catalog(lake_settings_iceberg, log_service=NoOpLogService())

    # Create namespaces
    for ns in [('raw',), ('normalized',)]:
        try:
            _catalog.create_namespace(ns)
        except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
            pass

    # raw.access_artifacts — string-partitioned (artifact_type instead of UUID)
    _raw_schema = Schema(
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
    _raw_spec = PartitionSpec(
        PartitionField(source_id=3, field_id=1000, transform=IdentityTransform(), name='artifact_type')
    )
    try:
        _catalog.create_table(('raw', 'access_artifacts'), schema=_raw_schema, partition_spec=_raw_spec)
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass

    # normalized.access_facts — string-based UUIDs, partitioned by subject_kind_denorm
    _norm_schema = Schema(
        NestedField(1, 'id', StringType(), required=True),
        NestedField(2, 'subject_id', StringType(), required=True),
        NestedField(3, 'account_id', StringType(), required=False),
        NestedField(4, 'resource_id', StringType(), required=True),
        NestedField(5, 'action_id', StringType(), required=True),
        NestedField(6, 'effect', StringType(), required=True),
        NestedField(7, 'valid_from', TimestamptzType(), required=False),
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
    _norm_spec = PartitionSpec(
        PartitionField(source_id=15, field_id=1001, transform=IdentityTransform(), name='subject_kind_denorm')
    )
    try:
        _catalog.create_table(('normalized', 'access_facts'), schema=_norm_schema, partition_spec=_norm_spec)
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass

    reset_catalog_cache_for_tests()

    # Build a DuckDB-compatible (libpq) DSN from the test DB URL.
    # Root conftest TEST_DATABASE_URL uses asyncpg scheme; strip the driver suffix.
    from src.conftest import TEST_DATABASE_URL  # noqa: PLC0415

    _pg_dsn = TEST_DATABASE_URL.replace('postgresql+asyncpg://', 'postgresql://', 1)

    _lake_factory = LakeSessionFactory(
        settings=lake_settings_iceberg,
        log_service=NoOpLogService(),
        pg_dsn=_pg_dsn,
    )

    def override_get_lake_session() -> Generator[Any]:
        session = _lake_factory.acquire()
        try:
            yield session
        finally:
            session.__exit__(None, None, None)

    # S4: wire event buffer as a tap on the 'noop' provider so emitted events land in buffer
    from src.platform.events.buffer import InMemoryEventBufferSink  # noqa: PLC0415
    from src.platform.events.factory import event_sink_factory  # noqa: PLC0415
    from src.platform.events.service import _NoOpEventSink  # noqa: PLC0415
    from src.platform.events.tee_sink import TeeEventSink  # noqa: PLC0415

    event_buffer = InMemoryEventBuffer()
    event_sink_factory.register(
        'noop',
        lambda: TeeEventSink(_NoOpEventSink(), InMemoryEventBufferSink(event_buffer)),
    )

    _app = FastAPI()
    _app.include_router(router, prefix='/api/v0')
    _app.dependency_overrides[get_db] = override_get_db
    _app.dependency_overrides[get_lake_session] = override_get_lake_session
    _app.state.log_service = NoOpLogService()
    _app.state.event_buffer = event_buffer
    _app.state.lake_session_factory = _lake_factory
    _app.state.lake_catalog = _catalog
    _app.state.lake_settings = lake_settings_iceberg
    _app.state.test_iceberg_catalog = _catalog
    _app.state.test_lake_settings = lake_settings_iceberg

    try:
        yield _app
    finally:
        # Restore default 'noop' provider so other tests are not affected
        event_sink_factory.register('noop', lambda: _NoOpEventSink())
        reset_catalog_cache_for_tests()


@pytest_asyncio.fixture
async def client_iceberg(app_iceberg: FastAPI) -> AsyncGenerator[AsyncClient]:
    """AsyncClient backed by app_iceberg."""
    async with AsyncClient(
        transport=ASGITransport(app=app_iceberg),
        base_url='http://testserver',
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def seed_pipeline_inventory(
    session: AsyncSession,
    dataset: dict[str, Any],
) -> dict[str, Any]:
    """Insert Application, Subjects, Accounts, Resources from dataset seed section.

    Returns mapping with resolved UUIDs:
    ``{'app_id', 'subject_ids', 'account_ids', 'resource_ids', 'action_ids'}``.
    """
    from src.inventory.accounts.models import Account
    from src.inventory.actions.models import Action as RefAction
    from src.inventory.employees.repository import create_employee
    from src.inventory.nhi.repository import create_nhi
    from src.inventory.persons.repository import create_person
    from src.inventory.resources.models import Resource
    from src.inventory.subjects.models import Subject, SubjectKind
    from src.platform.applications.models import Application

    seed = dataset['seed']

    # Application (unique name per test run)
    app_suffix = uuid.uuid4().hex[:6]
    app = Application(
        name=f'{seed["application"]["name"]}-{app_suffix}',
        code=f'{seed["application"]["code"]}-{app_suffix}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()

    # Subjects
    subject_ids: list[uuid.UUID] = []
    for s in seed['subjects']:
        kind = s['kind']
        if kind == 'employee':
            person = await create_person(session, external_id=f'{s["external_id"]}-{app_suffix}', full_name='p15 test')
            await session.flush()
            emp = await create_employee(session, person_id=person.id)
            await session.flush()
            subj = Subject(
                external_id=f'{s["external_id"]}-{app_suffix}',
                kind=SubjectKind.employee,
                principal_employee_id=emp.id,
                status='active',
            )
        else:
            # NHI subject — create NHI row first (mirrors employee branch pattern)
            nhi = await create_nhi(
                session,
                external_id=f'{s["external_id"]}-{app_suffix}',
                name=f'{s["external_id"]}-{app_suffix}',
                kind='service_account',
            )
            await session.flush()
            subj = Subject(
                external_id=f'{s["external_id"]}-{app_suffix}',
                kind=SubjectKind.nhi,
                principal_nhi_id=nhi.id,
                status='active',
                nhi_kind='service_account',
            )
        session.add(subj)
        await session.flush()
        subject_ids.append(subj.id)

    # Accounts
    account_ids: list[uuid.UUID] = []
    for a in seed['accounts']:
        acct = Account(
            application_id=app.id,
            subject_id=subject_ids[a['subject_idx']],
            username=f'{a["username"]}-{app_suffix}',
        )
        session.add(acct)
        await session.flush()
        account_ids.append(acct.id)

    # Resources
    resource_ids: list[uuid.UUID] = []
    resource_keys: list[str] = []
    resource_types: list[str] = []
    for r in seed['resources']:
        res = Resource(
            external_id=f'{r["external_id"]}-{app_suffix}',
            application_id=app.id,
            kind=r['kind'],
            resource_type=r['resource_type'],
            resource_key=f'{r["resource_key"]}-{app_suffix}',
        )
        session.add(res)
        await session.flush()
        resource_ids.append(res.id)
        resource_keys.append(f'{r["resource_key"]}-{app_suffix}')
        resource_types.append(r['resource_type'])

    # Resolve ref_action IDs
    action_ids: dict[str, int] = {}
    for slug in seed['ref_actions']:
        row = (await session.execute(sa.select(RefAction.id).where(RefAction.slug == slug))).scalar_one_or_none()
        if row is not None:
            action_ids[slug] = row

    return {
        'app_id': app.id,
        'subject_ids': subject_ids,
        'account_ids': account_ids,
        'resource_ids': resource_ids,
        'resource_keys': resource_keys,
        'resource_types': resource_types,
        'action_ids': action_ids,
        'app_suffix': app_suffix,
    }


def build_artifact_items(
    dataset: dict[str, Any],
    refs: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build bulk upsert items from dataset artifact definitions + resolved refs.

    Returns list of dicts suitable for ``AccessArtifactBulkItem`` serialization.
    """
    now_iso = datetime(2026, 4, 27, 12, 0, 0, tzinfo=UTC).isoformat()
    items = []
    for art in dataset['artifacts']:
        subject_id = refs['subject_ids'][art['subject_idx']]
        resource_key = refs['resource_keys'][art['resource_idx']]
        resource_type = refs['resource_types'][art['resource_idx']]
        action_slug = art['action_slug']
        items.append(
            {
                'application_id': str(refs['app_id']),
                'artifact_type': art['artifact_type'],
                'external_id': f'{art["external_id"]}-{refs["app_suffix"]}',
                'payload': {
                    'subject_id': str(subject_id),
                    'resource_type': resource_type,
                    'resource_key': resource_key,
                    'action_slug': action_slug,
                    'effect': art['effect'],
                    'valid_from': now_iso,
                },
                'raw_name': art['external_id'],
                'effect': art['effect'],
                'valid_from': now_iso,
                'valid_until': None,
                'observed_at': now_iso,
            }
        )
    return items


def get_event_types(event_buffer: InMemoryEventBuffer) -> list[str]:
    """Extract event_type strings from InMemoryEventBuffer snapshot."""
    return [e.event_type for e in event_buffer.snapshot(limit=500)]

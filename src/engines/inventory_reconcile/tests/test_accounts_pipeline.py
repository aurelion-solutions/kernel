# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for master-data accounts reconciliation pipeline.

Checks that run_accounts_reconciliation correctly:
  - creates delta items for new lake rows not in PG
  - produces update deltas when tracked fields differ
  - produces revoke deltas for PG accounts absent from lake
  - produces noop (unchanged_count) when everything matches
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
import uuid

import pyarrow as pa
import pytest
from src.engines.inventory_reconcile.master_data_pipeline import (
    MasterDataReconciliationResult,
    run_accounts_reconciliation,
    run_master_data_reconciliation,
)
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
)
from src.engines.inventory_reconcile.repository import create_run
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSessionFactory
from src.platform.lake.schemas import RAW_ACCOUNTS_SCHEMA, RAW_ACCOUNTS_TABLE
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
def lake_settings_iceberg(tmp_path: Path) -> LakeSettings:
    return LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/catalog.db',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
        artifacts_write_backend='iceberg',
    )


@pytest.fixture
def log_service() -> LogService:
    return LogService(sink=CapturingLogSink())


def _now_us() -> int:
    now = datetime.now(UTC)
    return int(now.timestamp() * 1_000_000)


def _make_accounts_arrow(items: list[dict[str, Any]], pa_schema: Any) -> Any:
    tz = pa.timestamp('us', tz='UTC')
    now_us = _now_us()
    batch_id = str(uuid.uuid4())

    ids, ingest_batch_ids, app_ids, usernames = [], [], [], []
    ext_ids, display_names, emails, statuses = [], [], [], []
    is_privilegeds, mfa_enableds, metas = [], [], []
    observed_ats, inserted_ats, is_actives, tombstoned_ats, nk_hints = [], [], [], [], []

    for item in items:
        ids.append(str(uuid.uuid4()))
        ingest_batch_ids.append(batch_id)
        app_ids.append(str(item['application_id']))
        usernames.append(item['username'])
        ext_ids.append(item.get('external_id'))
        display_names.append(item.get('display_name'))
        emails.append(item.get('email'))
        statuses.append(item.get('status', 'active'))
        is_privilegeds.append(item.get('is_privileged', False))
        mfa_enableds.append(item.get('mfa_enabled', False))
        metas.append(None)
        observed_ats.append(now_us)
        inserted_ats.append(now_us)
        is_actives.append(item.get('is_active', True))
        tombstoned_ats.append(None)
        nk_hints.append(f'{item["application_id"]}::{item["username"]}')

    return pa.table(
        {
            'id': pa.array(ids, type=pa.string()),
            'ingest_batch_id': pa.array(ingest_batch_ids, type=pa.string()),
            'application_id': pa.array(app_ids, type=pa.string()),
            'username': pa.array(usernames, type=pa.string()),
            'external_id': pa.array(ext_ids, type=pa.string()),
            'display_name': pa.array(display_names, type=pa.string()),
            'email': pa.array(emails, type=pa.string()),
            'status': pa.array(statuses, type=pa.string()),
            'is_privileged': pa.array(is_privilegeds, type=pa.bool_()),
            'mfa_enabled': pa.array(mfa_enableds, type=pa.bool_()),
            'meta': pa.array(metas, type=pa.string()),
            'observed_at': pa.array(observed_ats, type=tz),
            '_inserted_at': pa.array(inserted_ats, type=tz),
            'is_active': pa.array(is_actives, type=pa.bool_()),
            'tombstoned_at': pa.array(tombstoned_ats, type=tz),
            '_natural_key_hint': pa.array(nk_hints, type=pa.string()),
        },
        schema=pa_schema,
    )


@pytest.fixture
def seeded_accounts_lake(lake_settings_iceberg: LakeSettings, log_service: LogService):
    """Provision raw.accounts with 2 active rows. Returns (catalog, lake_session, app_id)."""
    cat = get_catalog(lake_settings_iceberg, log_service=log_service)
    for ns in (('raw',),):
        try:
            cat.create_namespace(ns)
        except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
            pass
    try:
        cat.drop_table(RAW_ACCOUNTS_TABLE)
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass

    # Use a stable UUID so tests can reference it as a foreign key in Applications
    app_id = uuid.uuid4()
    tbl = cat.create_table(RAW_ACCOUNTS_TABLE, schema=RAW_ACCOUNTS_SCHEMA)
    pa_schema = tbl.schema().as_arrow()
    arrow = _make_accounts_arrow(
        [
            {'application_id': app_id, 'username': 'alice', 'email': 'alice@x.com', 'mfa_enabled': False},
            {'application_id': app_id, 'username': 'bob', 'email': 'bob@x.com', 'mfa_enabled': True},
        ],
        pa_schema,
    )
    tbl.append(arrow)

    factory = LakeSessionFactory(settings=lake_settings_iceberg, log_service=log_service)
    lake_session = factory.acquire()
    yield cat, lake_session, app_id


async def _seed_application(session, app_id: uuid.UUID) -> None:
    """Seed an Application row with the given id."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.platform.applications.models import Application  # noqa: PLC0415

    existing = (await session.execute(sa.select(Application).where(Application.id == app_id))).scalar_one_or_none()
    if existing is None:
        session.add(Application(id=app_id, name=f'test-app-{app_id}', code=f'TEST-{str(app_id)[:8]}', config={}))
        await session.flush()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_accounts_pipeline_creates_when_pg_empty(session_factory, seeded_accounts_lake) -> None:
    """Lake accounts without PG match → all create deltas."""
    _cat, lake_session, _app_id = seeded_accounts_lake

    async with session_factory() as session:
        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.account)
        await session.flush()
        result = await run_accounts_reconciliation(session, lake_session, run=run)
        await session.commit()

    assert isinstance(result, MasterDataReconciliationResult)
    assert result.entity_type == ReconciliationEntityType.account
    assert result.created_count == 2
    assert result.updated_count == 0
    assert result.revoked_count == 0
    assert result.unchanged_count == 0


@pytest.mark.asyncio
async def test_accounts_pipeline_unchanged_when_pg_matches(session_factory, seeded_accounts_lake) -> None:
    """PG accounts matching lake → unchanged, no delta items persisted."""
    from src.inventory.accounts.models import Account, AccountStatus  # noqa: PLC0415

    _cat, lake_session, app_id = seeded_accounts_lake

    async with session_factory() as session:
        await _seed_application(session, app_id)
        session.add_all(
            [
                Account(
                    application_id=app_id,
                    username='alice',
                    email='alice@x.com',
                    mfa_enabled=False,
                    status=AccountStatus.active,
                ),
                Account(
                    application_id=app_id,
                    username='bob',
                    email='bob@x.com',
                    mfa_enabled=True,
                    status=AccountStatus.active,
                ),
            ]
        )
        await session.flush()

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.account)
        await session.flush()
        result = await run_accounts_reconciliation(session, lake_session, run=run)
        await session.commit()

    assert result.unchanged_count == 2
    assert result.created_count == 0
    assert result.updated_count == 0
    assert result.revoked_count == 0


@pytest.mark.asyncio
async def test_accounts_pipeline_update_when_mfa_changed(session_factory, seeded_accounts_lake) -> None:
    """mfa_enabled changed → update delta for that account."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.engines.inventory_reconcile.models import ReconciliationDeltaItem  # noqa: PLC0415
    from src.inventory.accounts.models import Account, AccountStatus  # noqa: PLC0415

    _cat, lake_session, app_id = seeded_accounts_lake

    async with session_factory() as session:
        await _seed_application(session, app_id)
        session.add_all(
            [
                Account(
                    application_id=app_id,
                    username='alice',
                    email='alice@x.com',
                    mfa_enabled=True,  # lake has False → update
                    status=AccountStatus.active,
                ),
                Account(
                    application_id=app_id,
                    username='bob',
                    email='bob@x.com',
                    mfa_enabled=True,  # lake also has True → unchanged
                    status=AccountStatus.active,
                ),
            ]
        )
        await session.flush()

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.account)
        await session.flush()
        result = await run_accounts_reconciliation(session, lake_session, run=run)
        await session.flush()

        delta_rows = (
            (
                await session.execute(
                    sa.select(ReconciliationDeltaItem).where(
                        ReconciliationDeltaItem.reconciliation_run_id == run.id,
                        ReconciliationDeltaItem.operation == ReconciliationDeltaOperation.update,
                    )
                )
            )
            .scalars()
            .all()
        )
        await session.commit()

    assert result.updated_count == 1
    assert result.unchanged_count == 1
    assert len(delta_rows) == 1


@pytest.mark.asyncio
async def test_accounts_pipeline_revoke_when_pg_not_in_lake(session_factory, seeded_accounts_lake) -> None:
    """PG account absent from lake → revoke delta."""
    import sqlalchemy as sa  # noqa: PLC0415
    from src.engines.inventory_reconcile.models import ReconciliationDeltaItem  # noqa: PLC0415
    from src.inventory.accounts.models import Account, AccountStatus  # noqa: PLC0415

    _cat, lake_session, app_id = seeded_accounts_lake

    async with session_factory() as session:
        await _seed_application(session, app_id)
        # 'charlie' is in PG but NOT in the lake
        session.add(
            Account(
                application_id=app_id,
                username='charlie',
                email='charlie@x.com',
                status=AccountStatus.active,
            )
        )
        await session.flush()

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.account)
        await session.flush()
        result = await run_accounts_reconciliation(session, lake_session, run=run)
        await session.flush()

        delta_rows = (
            (
                await session.execute(
                    sa.select(ReconciliationDeltaItem).where(
                        ReconciliationDeltaItem.reconciliation_run_id == run.id,
                        ReconciliationDeltaItem.operation == ReconciliationDeltaOperation.revoke,
                    )
                )
            )
            .scalars()
            .all()
        )
        await session.commit()

    # lake has alice + bob (creates) + charlie revoke
    assert result.created_count == 2
    assert result.revoked_count == 1
    assert len(delta_rows) == 1


@pytest.mark.asyncio
async def test_run_master_data_reconciliation_dispatches_account(session_factory, seeded_accounts_lake) -> None:
    """run_master_data_reconciliation with entity_type=account dispatches correctly."""
    _cat, lake_session, _app_id = seeded_accounts_lake

    async with session_factory() as session:
        result = await run_master_data_reconciliation(
            session, lake_session, entity_type=ReconciliationEntityType.account
        )
        await session.commit()

    assert result.entity_type == ReconciliationEntityType.account
    assert isinstance(result, MasterDataReconciliationResult)

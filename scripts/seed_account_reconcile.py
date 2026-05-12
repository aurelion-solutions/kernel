#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1
# ruff: noqa: E402
"""
Account reconciliation seed — lake-first flow.

Run from aurelion-kernel/:
    uv run python scripts/seed_account_reconcile.py

Flow:
  1. Seed raw.accounts in the Iceberg lake via AccountLakeService:
     - 2 new usernames (not in PG) → will become create deltas
     - 2 existing accounts with changed attributes → update / revoke deltas
     - 1 account with unchanged fields (if found) → noop
  2. Create a MasterDataRun for entity_type=account.
  3. Run generates ReconciliationDeltaItems automatically.
  4. Does NOT apply — delta items left as pending for UI demonstration.

Idempotent: lake rows deduplicated by natural key (retire-on-re-upload).
  Runs skipped if already exist (checked via seed marker in run.error field).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import importlib

from dotenv import load_dotenv

load_dotenv()
from src.platform.secrets.factory import register_default_providers

register_default_providers()
from src.core.config import get_settings

settings = get_settings()

import src.engines
import src.inventory
import src.platform

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
for _pkg in (src.inventory, src.engines, src.platform):
    for _root in map(Path, _pkg.__path__):
        for _p in _root.rglob('models.py'):
            importlib.import_module('.'.join(_p.relative_to(_PROJECT_ROOT).with_suffix('').parts))

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from src.engines.inventory_reconcile.master_data_pipeline import run_master_data_reconciliation
from src.engines.inventory_reconcile.models import (
    ReconciliationEntityType,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from src.inventory.accounts.lake_service import AccountLakeService
from src.inventory.accounts.models import Account, AccountStatus
from src.inventory.accounts.schemas import AccountBulkItem
from src.platform.applications.models import Application
from src.platform.logs.service import LogService

_SEED_RUN_MARKER = 'seed-account-reconcile-lake-v1'


def _skip(label: str) -> None:
    print(f'  SKIP (exists): {label}')


def _created(label: str) -> None:
    print(f'  CREATED: {label}')


# ---------------------------------------------------------------------------
# Step 1: Upload raw accounts to lake
# ---------------------------------------------------------------------------


async def seed_lake_accounts(
    lake_catalog,
    ghe_app: Application,
    ghe_accounts: dict[str, Account | None],
) -> None:
    """Upload account observations to raw.accounts lake table."""
    svc = AccountLakeService(lake_catalog=lake_catalog)

    ghe_id = ghe_app.id
    alexei = ghe_accounts.get('alexei.voronov')
    maria = ghe_accounts.get('maria.sokolova')
    pavel = ghe_accounts.get('pavel.morozov')

    items: list[AccountBulkItem] = [
        # 2 new accounts (not in PG) → will produce create deltas
        AccountBulkItem(
            application_id=ghe_id,
            username='new.hire1',
            email='new.hire1@company.com',
            display_name='New Hire 1',
            status=AccountStatus.active,
            is_privileged=False,
            mfa_enabled=True,
        ),
        AccountBulkItem(
            application_id=ghe_id,
            username='new.hire2',
            email='new.hire2@company.com',
            display_name='New Hire 2',
            status=AccountStatus.active,
            is_privileged=False,
            mfa_enabled=False,
        ),
    ]

    # Existing account: mfa flipped → update delta
    if alexei is not None:
        items.append(
            AccountBulkItem(
                application_id=ghe_id,
                username='alexei.voronov',
                email=alexei.email,
                display_name=alexei.display_name,
                status=AccountStatus(str(alexei.status)) if alexei.status else AccountStatus.active,
                is_privileged=alexei.is_privileged,
                mfa_enabled=not alexei.mfa_enabled,  # flip mfa to trigger update delta
            )
        )

    # Existing account: display_name changed → update delta
    if maria is not None:
        items.append(
            AccountBulkItem(
                application_id=ghe_id,
                username='maria.sokolova',
                email=maria.email,
                display_name='Maria Sokolova-Ivanova',  # changed
                status=AccountStatus(str(maria.status)) if maria.status else AccountStatus.active,
                is_privileged=maria.is_privileged,
                mfa_enabled=maria.mfa_enabled,
            )
        )

    # Existing account: observed as disabled → revoke delta
    if pavel is not None:
        items.append(
            AccountBulkItem(
                application_id=ghe_id,
                username='pavel.morozov',
                email=pavel.email,
                display_name=pavel.display_name,
                status=AccountStatus.disabled,  # was active → revoke
                is_privileged=pavel.is_privileged,
                mfa_enabled=pavel.mfa_enabled,
            )
        )

    result = await svc.upsert_batch(items, ingest_batch_id=uuid.uuid4())
    _created(f'raw.accounts: {result.row_count} rows written, snapshot_id={result.snapshot_id}')


# ---------------------------------------------------------------------------
# Step 2: Create master data reconcile run
# ---------------------------------------------------------------------------


async def seed_account_reconcile_run(
    session: AsyncSession,
    lake_session,
) -> None:
    """Create a master-data reconciliation run for entity_type=account."""
    # Check if seed run already exists
    r = await session.execute(
        sa.select(ReconciliationRun).where(
            ReconciliationRun.entity_type == ReconciliationEntityType.account,
            ReconciliationRun.error == _SEED_RUN_MARKER,
            ReconciliationRun.status == ReconciliationRunStatus.pending_apply,
        )
    )
    existing = r.scalar_one_or_none()
    if existing is not None:
        _skip(f'ReconciliationRun account ({_SEED_RUN_MARKER})')
        return

    result = await run_master_data_reconciliation(
        session,
        lake_session,
        entity_type=ReconciliationEntityType.account,
    )

    # Tag the run with seed marker for idempotency on re-run
    r2 = await session.execute(sa.select(ReconciliationRun).where(ReconciliationRun.id == result.run_id))
    run = r2.scalar_one()
    run.error = _SEED_RUN_MARKER  # use error field as idempotency tag
    await session.flush()

    _created(
        f'ReconciliationRun account run_id={result.run_id}: '
        f'created={result.created_count}, updated={result.updated_count}, '
        f'revoked={result.revoked_count}, unchanged={result.unchanged_count}'
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main() -> None:
    from src.platform.lake.catalog import get_catalog
    from src.platform.lake.config import build_lake_settings
    from src.platform.lake.duckdb_session import LakeSessionFactory
    from src.platform.lake.provisioning import ensure_tables
    from src.platform.runtime_settings.service import RuntimeSettingsService

    class _NullSink:
        def send(self, *a, **kw):  # noqa: ANN001
            pass

    _null_log = LogService(sink=_NullSink())  # type: ignore[arg-type]

    inner_engine = create_async_engine(settings.postgres.dsn, echo=False)
    inner_factory = async_sessionmaker(inner_engine, class_=AsyncSession, expire_on_commit=False)

    # Ensure runtime settings exist
    async with inner_factory() as _sess:
        _rt_svc = RuntimeSettingsService(_sess, _null_log)
        await _rt_svc.ensure_defaults()
        await _sess.commit()

    async with inner_factory() as _sess:
        _rt_svc = RuntimeSettingsService(_sess, _null_log)
        _runtime = await _rt_svc.load()

    lake_settings = build_lake_settings(
        settings.postgres,
        _runtime,
        catalog_name=settings.lake.catalog_name,
        warehouse_uri=settings.lake.warehouse_uri,
        storage_provider=settings.lake.storage_provider,  # type: ignore[arg-type]
        artifacts_write_backend=settings.lake.artifacts_write_backend,  # type: ignore[arg-type]
    )
    catalog = get_catalog(lake_settings, _null_log)
    ensure_tables(catalog, log_service=_null_log)
    await inner_engine.dispose()

    pg_dsn = settings.postgres.dsn.replace('+asyncpg', '').replace('+psycopg2', '')
    lake_factory = LakeSessionFactory(
        settings=lake_settings,
        log_service=_null_log,
        pg_dsn=pg_dsn,
    )

    engine = create_async_engine(settings.postgres.dsn, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    sep = '─' * 70
    print(sep)
    print('SEED ACCOUNT RECONCILE — lake-first flow')
    print(sep)

    async with factory() as session:
        # Load GHE application
        r = await session.execute(sa.select(Application).where(Application.code == 'GHE'))
        ghe = r.scalar_one_or_none()
        if ghe is None:
            print('  ERROR: GHE application not found. Run seed_dev.py first.')
            return

        # Load known GHE accounts for comparison
        known_usernames = ['alexei.voronov', 'maria.sokolova', 'pavel.morozov']
        ghe_accounts: dict[str, Account | None] = {}
        for uname in known_usernames:
            r2 = await session.execute(
                sa.select(Account).where(
                    Account.application_id == ghe.id,
                    Account.username == uname,
                )
            )
            ghe_accounts[uname] = r2.scalar_one_or_none()

        # Step 1: Upload to lake
        print('\n[Step 1] Uploading raw accounts to lake...')
        await seed_lake_accounts(catalog, ghe, ghe_accounts)

        # Step 2: Trigger reconcile run
        print('\n[Step 2] Creating account reconcile run...')
        lake_session = lake_factory.acquire()
        try:
            await seed_account_reconcile_run(session, lake_session)
        finally:
            lake_factory.release(lake_session)

        await session.commit()

    print()
    print(sep)
    print('SEED COMPLETE')
    print(sep)
    print('Verify:')
    print(
        "  curl 'http://localhost:8000/api/v0/inventory-reconciles/delta-items"
        "?entity_type=account&status=pending&limit=10' | jq '.items | length'"
    )
    print(sep)

    await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())

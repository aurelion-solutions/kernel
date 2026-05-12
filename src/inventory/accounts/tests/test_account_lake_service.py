# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for AccountLakeService — write + read latest snapshot."""

from __future__ import annotations

from pathlib import Path
import uuid

import pytest
from src.inventory.accounts.lake_service import AccountLakeNotConfiguredError, AccountLakeService
from src.inventory.accounts.models import AccountStatus
from src.inventory.accounts.schemas import AccountBulkItem
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
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
def lake_settings(tmp_path: Path) -> LakeSettings:
    return LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/catalog.db',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
        artifacts_write_backend='iceberg',
    )


@pytest.fixture
def log_service() -> LogService:
    return LogService(sink=CapturingLogSink())


@pytest.fixture
def catalog(lake_settings: LakeSettings, log_service: LogService):
    """Provision an isolated catalog with raw.accounts table."""
    cat = get_catalog(lake_settings, log_service=log_service)
    try:
        cat.create_namespace(('raw',))
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass
    try:
        cat.drop_table(RAW_ACCOUNTS_TABLE)
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass
    cat.create_table(RAW_ACCOUNTS_TABLE, schema=RAW_ACCOUNTS_SCHEMA)
    return cat


def _item(
    app_id: uuid.UUID,
    username: str,
    *,
    display_name: str | None = None,
    email: str | None = None,
    status: AccountStatus = AccountStatus.active,
    is_privileged: bool = False,
    mfa_enabled: bool = False,
) -> AccountBulkItem:
    return AccountBulkItem(
        application_id=app_id,
        username=username,
        display_name=display_name,
        email=email,
        status=status,
        is_privileged=is_privileged,
        mfa_enabled=mfa_enabled,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upsert_batch_returns_row_count(catalog) -> None:
    """upsert_batch returns row_count == len(items)."""
    svc = AccountLakeService(lake_catalog=catalog)
    app_id = uuid.uuid4()
    items = [
        _item(app_id, 'alice', display_name='Alice', email='alice@x.com'),
        _item(app_id, 'bob', display_name='Bob'),
    ]
    result = await svc.upsert_batch(items, ingest_batch_id=uuid.uuid4())
    assert result.row_count == 2
    assert result.backend == 'iceberg'


@pytest.mark.asyncio
async def test_upsert_batch_snapshot_id_is_set(catalog) -> None:
    """After write, snapshot_id is a non-None integer."""
    svc = AccountLakeService(lake_catalog=catalog)
    app_id = uuid.uuid4()
    result = await svc.upsert_batch(
        [_item(app_id, 'charlie')],
        ingest_batch_id=uuid.uuid4(),
    )
    assert result.snapshot_id is not None
    assert isinstance(result.snapshot_id, int)


@pytest.mark.asyncio
async def test_upsert_batch_data_readable(catalog) -> None:
    """Written rows are readable via DuckDB iceberg_scan."""

    svc = AccountLakeService(lake_catalog=catalog)
    app_id = uuid.uuid4()
    items = [_item(app_id, 'dave', display_name='Dave', email='dave@x.com', mfa_enabled=True)]
    await svc.upsert_batch(items, ingest_batch_id=uuid.uuid4())

    tbl = catalog.load_table(RAW_ACCOUNTS_TABLE)
    arrow = tbl.scan().to_arrow()
    assert len(arrow) == 1
    row = arrow.to_pydict()
    assert row['username'][0] == 'dave'
    assert row['email'][0] == 'dave@x.com'
    assert row['mfa_enabled'][0] is True
    assert row['is_active'][0] is True


@pytest.mark.asyncio
async def test_upsert_batch_retires_old_row_on_second_write(catalog) -> None:
    """Second write of same (application_id, username) → old row retired, new row active."""
    svc = AccountLakeService(lake_catalog=catalog)
    app_id = uuid.uuid4()

    # First write
    await svc.upsert_batch(
        [_item(app_id, 'eve', display_name='Eve Old')],
        ingest_batch_id=uuid.uuid4(),
    )
    # Second write — same natural key, different display_name
    await svc.upsert_batch(
        [_item(app_id, 'eve', display_name='Eve New')],
        ingest_batch_id=uuid.uuid4(),
    )

    tbl = catalog.load_table(RAW_ACCOUNTS_TABLE)
    arrow = tbl.scan().to_arrow()
    rows = arrow.to_pydict()

    total = len(rows['id'])
    active_count = sum(1 for a in rows['is_active'] if a)
    assert total == 2
    assert active_count == 1
    # Active row has the new display_name
    for i in range(total):
        if rows['is_active'][i]:
            assert rows['display_name'][i] == 'Eve New'


@pytest.mark.asyncio
async def test_upsert_batch_no_catalog_raises() -> None:
    """AccountLakeService without catalog raises AccountLakeNotConfiguredError."""
    svc = AccountLakeService(lake_catalog=None)
    with pytest.raises(AccountLakeNotConfiguredError):
        await svc.upsert_batch(
            [_item(uuid.uuid4(), 'x')],
            ingest_batch_id=uuid.uuid4(),
        )

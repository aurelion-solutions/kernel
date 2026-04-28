# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for src/platform/lake/catalog.py."""

from collections.abc import Generator
from pathlib import Path

import pytest
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.exceptions import LakeCatalogError
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink


@pytest.fixture
def capturing_log_service() -> tuple[LogService, CapturingLogSink]:
    sink = CapturingLogSink()
    return LogService(sink=sink), sink


@pytest.fixture
def lake_settings_sqlite(tmp_path: Path) -> LakeSettings:
    return LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/catalog.db',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
    )


@pytest.fixture(autouse=True)
def reset_cache() -> Generator[None]:
    reset_catalog_cache_for_tests()
    yield
    reset_catalog_cache_for_tests()


def test_catalog_returns_usable_catalog(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    log_service, _ = capturing_log_service
    catalog = get_catalog(lake_settings_sqlite, log_service)
    assert catalog is not None

    # Bootstrap creates raw and normalized namespaces automatically
    ns_list = [tuple(n) for n in catalog.list_namespaces()]
    assert ('raw',) in ns_list
    assert ('normalized',) in ns_list


def test_catalog_emits_initialized_log(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    log_service, sink = capturing_log_service
    get_catalog(lake_settings_sqlite, log_service)

    # emit_safe runs asyncio.run() synchronously when no loop is running,
    # so records are captured immediately after the call returns.
    initialized_records = [r for r in sink.records if 'catalog_initialized' in r.message]
    assert len(initialized_records) == 1
    assert initialized_records[0].payload.get('warehouse_uri') == lake_settings_sqlite.warehouse_uri


def test_catalog_caching_returns_same_instance(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    log_service, _ = capturing_log_service
    catalog1 = get_catalog(lake_settings_sqlite, log_service)
    catalog2 = get_catalog(lake_settings_sqlite, log_service)
    assert catalog1 is catalog2


def test_catalog_invalid_url_raises_lake_catalog_error(
    capturing_log_service: tuple[LogService, CapturingLogSink],
    tmp_path: Path,
) -> None:
    log_service, sink = capturing_log_service
    bad_settings = LakeSettings(
        catalog_url='not-a-valid-url://',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
    )
    with pytest.raises(LakeCatalogError):
        get_catalog(bad_settings, log_service)

    # emit_safe runs synchronously (no running loop), so records are captured immediately.
    failed_records = [r for r in sink.records if 'catalog_init_failed' in r.message]
    assert len(failed_records) == 1

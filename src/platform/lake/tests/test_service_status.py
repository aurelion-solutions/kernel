# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for src/platform/lake/service.py — get_lake_status."""

from __future__ import annotations

from pyiceberg.catalog import Catalog
from src.platform.lake.config import LakeSettings
from src.platform.lake.service import get_lake_status
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

# ---------------------------------------------------------------------------
# T1: deterministic ordering — normalized < raw lexicographically
# ---------------------------------------------------------------------------


def test_returns_table_metadata_in_deterministic_order(
    lake_catalog_with_tables: Catalog,
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """All 5 standard tables present; result order: sorted by (namespace, name)."""
    log, _ = capturing_log_service
    response = get_lake_status(lake_catalog_with_tables, lake_settings_sqlite, log_service=log)

    assert len(response.tables) == 5
    expected = [
        ('normalized', 'access_facts'),
        ('raw', 'access_artifacts'),
        ('raw', 'employees'),
        ('raw', 'org_units'),
        ('raw', 'persons'),
    ]
    for entry, (exp_ns, exp_name) in zip(response.tables, expected):
        assert entry.namespace == exp_ns
        assert entry.name == exp_name


# ---------------------------------------------------------------------------
# T2: freshly provisioned tables have zero snapshots
# ---------------------------------------------------------------------------


def test_snapshot_count_is_zero_for_freshly_provisioned_tables(
    lake_catalog_with_tables: Catalog,
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """No writes → snapshot_count == 0, current_snapshot_id is None, last_updated_ms is None."""
    log, _ = capturing_log_service
    response = get_lake_status(lake_catalog_with_tables, lake_settings_sqlite, log_service=log)

    for table in response.tables:
        assert table.snapshot_count == 0
        assert table.current_snapshot_id is None
        assert table.last_updated_ms is None


# ---------------------------------------------------------------------------
# T3: catalog URI credential redaction
# ---------------------------------------------------------------------------


def test_redacts_credentials_in_catalog_uri(
    capturing_log_service: tuple[LogService, CapturingLogSink],
    lake_catalog_with_tables: Catalog,
) -> None:
    """Password must be stripped from catalog_uri; username and host must remain."""
    log, _ = capturing_log_service
    settings = LakeSettings(
        catalog_url='postgresql://user:pass@host:5432/db',
        warehouse_uri='file:///tmp/warehouse',
        storage_provider='file',
    )
    response = get_lake_status(lake_catalog_with_tables, settings, log_service=log)

    assert 'pass' not in response.catalog_uri
    assert 'user@host' in response.catalog_uri


# ---------------------------------------------------------------------------
# T4: empty catalog returns empty table list
# ---------------------------------------------------------------------------


def test_empty_catalog_returns_empty_table_list(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """Fresh SQLite catalog without ensure_tables → tables == [], log still emitted."""
    from src.platform.lake.catalog import get_catalog

    log, sink = capturing_log_service
    # Fresh catalog — no tables provisioned.
    catalog = get_catalog(lake_settings_sqlite, log_service=log)
    sink.clear()

    response = get_lake_status(catalog, lake_settings_sqlite, log_service=log)

    assert response.tables == []
    assert response.warehouse_uri == lake_settings_sqlite.warehouse_uri
    assert response.storage_provider == lake_settings_sqlite.storage_provider

    status_logs = [r for r in sink.records if r.message == 'platform.lake.status_queried']
    assert len(status_logs) == 1


# ---------------------------------------------------------------------------
# T5: exactly one INFO log emitted with correct payload
# ---------------------------------------------------------------------------


def test_emits_exactly_one_info_log(
    lake_catalog_with_tables: Catalog,
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> None:
    """Service emits exactly one LogEvent with the correct message, level, and table_count."""
    log, sink = capturing_log_service
    sink.clear()

    response = get_lake_status(lake_catalog_with_tables, lake_settings_sqlite, log_service=log)

    status_logs = [r for r in sink.records if r.message == 'platform.lake.status_queried']
    assert len(status_logs) == 1
    event = status_logs[0]
    assert event.level == LogLevel.INFO
    assert event.payload['table_count'] == len(response.tables)

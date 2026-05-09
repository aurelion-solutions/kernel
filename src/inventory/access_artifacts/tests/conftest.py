# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Shared fixtures for access_artifacts tests — lake-path additions (Phase 15 Step 5)."""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from src.platform.lake.catalog import get_catalog
from src.platform.lake.catalog import reset_catalog_cache_for_tests as _reset_catalog_cache
from src.platform.lake.config import LakeSettings
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog


@pytest.fixture
def capturing_log_service() -> tuple[LogService, CapturingLogSink]:
    sink = CapturingLogSink()
    return LogService(sink=sink), sink


@pytest.fixture
def lake_settings_iceberg(tmp_path: Path) -> LakeSettings:
    """LakeSettings backed by an in-process SQLite catalog with iceberg write backend."""
    return LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/catalog.db',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
        artifacts_write_backend='iceberg',
    )


@pytest.fixture
def lake_settings_pg(tmp_path: Path) -> LakeSettings:
    """LakeSettings backed by an in-process SQLite catalog with pg write backend."""
    return LakeSettings(
        catalog_url=f'sqlite:///{tmp_path}/catalog.db',
        warehouse_uri=f'file://{tmp_path}/warehouse',
        storage_provider='file',
        artifacts_write_backend='pg',
    )


@pytest.fixture(autouse=True)
def reset_cache() -> Generator[None]:
    """Reset catalog cache before and after each test to prevent cross-test pollution."""
    _reset_catalog_cache()
    yield
    _reset_catalog_cache()


@pytest.fixture
def lake_catalog_with_tables(
    lake_settings_iceberg: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> Catalog:
    """Catalog with raw.access_artifacts table provisioned.

    NOTE: This provisions the real ``RAW_ACCESS_ARTIFACTS_SCHEMA`` (UUID partitions).
    Due to PyArrow 24 limitation (``group_by`` on ``extension<arrow.uuid>`` not supported),
    this catalog cannot be used with ``table.append()``.  Use ``artifacts_table_fixture``
    instead for write-path tests.
    """
    log, _ = capturing_log_service
    from src.platform.lake.provisioning import ensure_tables

    catalog = get_catalog(lake_settings_iceberg, log_service=log)
    ensure_tables(catalog, log_service=log)
    return catalog


@pytest.fixture
def artifacts_table_fixture(
    lake_settings_iceberg: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> object:
    """Create a test-only ``raw.access_artifacts``-shaped table with string partitions.

    PyArrow 24 does not support ``group_by`` on ``extension<arrow.uuid>`` columns,
    which PyIceberg uses for UUID partition fields in the production schema.
    This fixture creates a structurally identical table but uses
    ``artifact_type`` (string) as the sole partition key.  The PyArrow schema uses
    plain string types for all UUID columns so that PyIceberg can group_by correctly.

    The fixture monkey-patches ``RAW_ACCESS_ARTIFACTS_TABLE`` in the catalog so the
    service can load it via the same identifier.
    """
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.transforms import IdentityTransform
    from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType

    log, _ = capturing_log_service
    catalog = get_catalog(lake_settings_iceberg, log_service=log)

    # Create 'raw' namespace if absent
    try:
        catalog.create_namespace(('raw',))
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass

    # Drop the table if it already exists (idempotent fixture)
    try:
        catalog.drop_table(('raw', 'access_artifacts'))
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass

    test_schema = Schema(
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
    test_spec = PartitionSpec(
        PartitionField(
            source_id=3,
            field_id=1000,
            transform=IdentityTransform(),
            name='artifact_type',
        )
    )
    catalog.create_table(
        ('raw', 'access_artifacts'),
        schema=test_schema,
        partition_spec=test_spec,
    )
    return catalog

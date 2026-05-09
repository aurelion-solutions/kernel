# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Shared fixtures for platform/lake test suite."""

from collections.abc import Generator
from pathlib import Path
from typing import TYPE_CHECKING

from pyiceberg.catalog import Catalog
import pytest
from src.platform.lake.catalog import get_catalog, reset_catalog_cache_for_tests
from src.platform.lake.config import LakeSettings
from src.platform.lake.provisioning import ensure_tables
from src.platform.logs.service import LogService
from src.platform.logs.testing import CapturingLogSink

if TYPE_CHECKING:
    from pyiceberg.table import Table


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


@pytest.fixture
def lake_catalog_with_tables(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> Catalog:
    """Return a Catalog with both standard lake tables provisioned.

    Uses the SQLite in-memory catalog from ``lake_settings_sqlite``.
    Encapsulates the ``get_catalog`` + ``ensure_tables`` bootstrap that most
    lake service tests need.
    """
    log, _ = capturing_log_service
    catalog = get_catalog(lake_settings_sqlite, log_service=log)
    ensure_tables(catalog, log_service=log)
    return catalog


@pytest.fixture
def maintenance_table(
    lake_settings_sqlite: LakeSettings,
    capturing_log_service: tuple[LogService, CapturingLogSink],
) -> Generator['Table']:
    """Yield a PyIceberg ``Table`` handle suitable for maintenance function tests.

    Uses a minimal test-only Iceberg table (``test.artifacts``) with a single
    ``artifact_type`` (string) partition.  A string partition is required because
    PyArrow 24 does not support ``group_by`` on ``extension<arrow.uuid>`` columns,
    which PyIceberg uses for UUID partition fields.  Using a string partition avoids
    this limitation while still exercising all three maintenance functions against a
    realistic multi-snapshot, multi-file Iceberg table.

    The ``ensure_tables`` call provisions ``raw.access_artifacts`` as a side-effect
    so the catalog is fully bootstrapped and the table identifier ``raw.access_artifacts``
    is accessible via ``catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)`` in other tests
    that do not perform data writes.
    """
    from pyiceberg.partitioning import PartitionField, PartitionSpec
    from pyiceberg.schema import Schema
    from pyiceberg.transforms import IdentityTransform
    from pyiceberg.types import BooleanType, NestedField, StringType, TimestamptzType

    log, _ = capturing_log_service

    # Bootstrap the standard lake tables so the catalog is complete.
    catalog = get_catalog(lake_settings_sqlite, log_service=log)
    ensure_tables(catalog, log_service=log)

    # Create a lightweight test table with a string-only partition.
    # The ``artifact_type`` column acts as the sole partition key; all other
    # columns are strings or timestamps to keep the schema simple.
    test_schema = Schema(
        NestedField(1, 'id', StringType(), required=True),
        NestedField(2, 'application_id', StringType(), required=True),
        NestedField(3, 'artifact_type', StringType(), required=True),
        NestedField(4, 'external_id', StringType(), required=False),
        NestedField(5, 'is_active', BooleanType(), required=True),
        NestedField(6, 'observed_at', TimestamptzType(), required=True),
        NestedField(7, 'ingested_at', TimestamptzType(), required=True),
    )
    test_spec = PartitionSpec(
        PartitionField(source_id=3, field_id=1000, transform=IdentityTransform(), name='artifact_type'),
    )
    test_identifier = ('test', 'artifacts')

    try:
        catalog.create_namespace(('test',))
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass  # namespace may already exist

    try:
        catalog.drop_table(test_identifier)
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        pass

    tbl = catalog.create_table(test_identifier, schema=test_schema, partition_spec=test_spec)
    yield tbl

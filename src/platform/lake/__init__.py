# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Public API for the lake infrastructure slice (Layer 1)."""

from src.platform.lake.catalog import get_catalog
from src.platform.lake.config import LakeSettings
from src.platform.lake.deps import get_lake_catalog, get_lake_session, get_lake_settings
from src.platform.lake.duckdb_session import LakeSession, LakeSessionFactory
from src.platform.lake.exceptions import (
    LakeCatalogError,
    LakeError,
    LakeMaintenanceError,
    LakeSessionError,
    LakeSessionPoolExhaustedError,
)
from src.platform.lake.maintenance import (
    CleanOrphanFilesResult,
    CompactTableResult,
    ExpireSnapshotsResult,
    clean_orphan_files,
    compact_table,
    expire_old_snapshots,
)
from src.platform.lake.provisioning import EnsureTablesResult, EnsuredTable, ensure_tables
from src.platform.lake.read_schemas import LakeStatusResponse, LakeTableStatus
from src.platform.lake.service import get_lake_status
from src.platform.lake.schemas import (
    NORMALIZED_ACCESS_FACTS_PARTITION_SPEC,
    NORMALIZED_ACCESS_FACTS_SCHEMA,
    NORMALIZED_ACCESS_FACTS_TABLE,
    NORMALIZED_NAMESPACE,
    RAW_ACCESS_ARTIFACTS_PARTITION_SPEC,
    RAW_ACCESS_ARTIFACTS_SCHEMA,
    RAW_ACCESS_ARTIFACTS_TABLE,
    RAW_NAMESPACE,
)

__all__ = [
    'LakeSettings',
    'get_catalog',
    'ensure_tables',
    'EnsureTablesResult',
    'EnsuredTable',
    'LakeSession',
    'LakeSessionFactory',
    'get_lake_catalog',
    'get_lake_session',
    'get_lake_settings',
    'LakeStatusResponse',
    'LakeTableStatus',
    'get_lake_status',
    'LakeError',
    'LakeCatalogError',
    'LakeSessionError',
    'LakeSessionPoolExhaustedError',
    'LakeMaintenanceError',
    'compact_table',
    'expire_old_snapshots',
    'clean_orphan_files',
    'CompactTableResult',
    'ExpireSnapshotsResult',
    'CleanOrphanFilesResult',
    'RAW_NAMESPACE',
    'NORMALIZED_NAMESPACE',
    'RAW_ACCESS_ARTIFACTS_TABLE',
    'NORMALIZED_ACCESS_FACTS_TABLE',
    'RAW_ACCESS_ARTIFACTS_SCHEMA',
    'RAW_ACCESS_ARTIFACTS_PARTITION_SPEC',
    'NORMALIZED_ACCESS_FACTS_SCHEMA',
    'NORMALIZED_ACCESS_FACTS_PARTITION_SPEC',
]

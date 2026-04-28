# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Read-only response DTOs for the lake status and compaction endpoints."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LakeTableStatus(BaseModel):
    """Snapshot metadata for a single Iceberg table."""

    model_config = ConfigDict(frozen=True)

    namespace: str
    name: str
    current_snapshot_id: int | None
    snapshot_count: int
    last_updated_ms: int | None


class LakeStatusResponse(BaseModel):
    """Response payload for GET /lake/status."""

    model_config = ConfigDict(frozen=True)

    catalog_uri: str
    warehouse_uri: str
    storage_provider: Literal['file', 's3']
    tables: list[LakeTableStatus]


# ---------------------------------------------------------------------------
# Compaction request / response schemas
# ---------------------------------------------------------------------------


class LakeCompactionRequest(BaseModel):
    """Request body for POST /lake/compaction."""

    model_config = ConfigDict(frozen=True)

    table: Literal['raw.access_artifacts', 'normalized.access_facts', 'all'] = 'all'
    retention_days: int = Field(default=7, ge=1)
    orphan_older_than_hours: int = Field(default=24, ge=1)
    target_file_size_mb: int = Field(default=128, ge=16)


class LakeTableCompactionResult(BaseModel):
    """Per-table result from a compaction run."""

    model_config = ConfigDict(frozen=True)

    namespace: str
    name: str
    files_before: int
    files_after: int
    bytes_before: int
    bytes_after: int
    compaction_snapshot_id: int | None
    snapshots_removed: int
    latest_snapshot_id: int | None
    orphan_files_removed: int
    orphan_bytes_freed: int
    orphan_cleanup_skipped: bool
    orphan_cleanup_skip_reason: str | None


class LakeCompactionResponse(BaseModel):
    """Response payload for POST /lake/compaction."""

    model_config = ConfigDict(frozen=True)

    tables: list[LakeTableCompactionResult]
    orphan_cleanup_skipped: bool
    orphan_cleanup_skip_reason: str | None

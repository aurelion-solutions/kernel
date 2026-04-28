# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    'AccessArtifactCreate',
    'AccessArtifactRead',
    'AccessArtifactView',
    'AccessArtifactBulkItem',
    'AccessArtifactBulkUpsertRequest',
    'AccessArtifactBulkUpsertResponse',
    'AccessArtifactBulkTombstoneRequest',
    'AccessArtifactBulkTombstoneResponse',
    'AccessArtifactCursorPage',
]


class AccessArtifactCreate(BaseModel):
    """Internal schema for creating an access artifact. NOT exposed via REST."""

    application_id: uuid.UUID
    artifact_type: str = Field(..., min_length=1, max_length=255)
    external_id: str = Field(..., min_length=1, max_length=255)
    payload: dict[str, Any]
    raw_name: str | None = Field(None, max_length=255)
    effect: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    ingest_batch_id: str | None = Field(None, max_length=255)
    observed_at: datetime | None = None


class AccessArtifactView(BaseModel):
    """Frozen Pydantic v2 DTO for access artifact reads — replaces the deleted ORM model.

    Used as the return type of service read methods and as the handler contract
    input type (Phase 15 Step 16). 14 fields mirror the deleted ORM.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra='forbid')

    id: uuid.UUID
    application_id: uuid.UUID
    artifact_type: str
    external_id: str
    payload: dict[str, Any]
    raw_name: str | None
    effect: str | None
    valid_from: datetime | None
    valid_until: datetime | None
    is_active: bool
    tombstoned_at: datetime | None
    observed_at: datetime
    ingested_at: datetime
    ingest_batch_id: str | None


class AccessArtifactRead(BaseModel):
    """Response schema for access artifact endpoints."""

    id: uuid.UUID
    application_id: uuid.UUID
    artifact_type: str
    external_id: str
    payload: dict[str, Any]
    raw_name: str | None
    effect: str | None
    valid_from: datetime | None
    valid_until: datetime | None
    ingested_at: datetime
    ingest_batch_id: str | None
    observed_at: datetime
    is_active: bool
    tombstoned_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Bulk upsert schemas
# ---------------------------------------------------------------------------


class AccessArtifactBulkItem(BaseModel):
    """Single artifact item in a bulk upsert request."""

    application_id: uuid.UUID
    artifact_type: str = Field(..., min_length=1, max_length=255)
    external_id: str = Field(..., min_length=1, max_length=255)
    payload: dict[str, Any]
    raw_name: str | None = Field(None, max_length=255)
    effect: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    observed_at: datetime | None = None


class AccessArtifactBulkUpsertRequest(BaseModel):
    """Bulk upsert request: up to 10 000 items."""

    ingest_batch_id: uuid.UUID
    items: list[AccessArtifactBulkItem] = Field(min_length=1, max_length=10_000)
    correlation_id: str | None = None


class AccessArtifactBulkUpsertResponse(BaseModel):
    """Bulk upsert response."""

    row_count: int
    snapshot_id: int | None
    backend: Literal['iceberg']


# ---------------------------------------------------------------------------
# Bulk tombstone schemas
# ---------------------------------------------------------------------------


class AccessArtifactBulkTombstoneRequest(BaseModel):
    """Bulk tombstone request: up to 10 000 artifact IDs."""

    artifact_ids: list[uuid.UUID] = Field(min_length=1, max_length=10_000)
    observed_at: datetime
    correlation_id: str | None = None


class AccessArtifactBulkTombstoneResponse(BaseModel):
    """Bulk tombstone response."""

    tombstoned_count: int
    snapshot_id: int | None
    backend: Literal['iceberg']


# ---------------------------------------------------------------------------
# Cursor page schema
# ---------------------------------------------------------------------------


class AccessArtifactCursorPage(BaseModel):
    """Cursor-paginated response for GET /access-artifacts."""

    items: list[AccessArtifactView]
    next_cursor: str | None

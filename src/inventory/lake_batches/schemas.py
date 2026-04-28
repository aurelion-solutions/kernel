# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake batch API schemas."""

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class LakeBatchWriteRequest(BaseModel):
    """Request body for POST /api/v0/datalake/batches (write records to lake)."""

    storage_provider: str = Field(..., min_length=1, max_length=64)
    dataset_type: str = Field(..., min_length=1, max_length=64)
    records: list[dict[str, Any]] = Field(..., min_length=0)
    task_id: uuid.UUID | None = Field(default=None)
    application_id: uuid.UUID | None = Field(default=None)


class LakeBatchCreate(BaseModel):
    """Request body for creating a lake batch reference."""

    storage_provider: str = Field(..., min_length=1, max_length=64)
    dataset_type: str = Field(..., min_length=1, max_length=64)
    storage_key: str = Field(..., min_length=1, max_length=512)
    row_count: int = Field(..., ge=0)
    application_id: uuid.UUID | None = Field(default=None)
    task_id: uuid.UUID | None = Field(default=None)


class LakeBatchRead(BaseModel):
    """Lake batch metadata response."""

    id: uuid.UUID
    storage_provider: str | None = None
    dataset_type: str
    storage_key: str | None = None
    row_count: int
    created_at: datetime
    application_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    content_type: str | None = None
    metadata_json: dict[str, Any] | None = None
    iceberg_namespace: str | None = None
    iceberg_table: str | None = None
    snapshot_id: int | None = None

    model_config = ConfigDict(from_attributes=True)

    @field_serializer('snapshot_id')
    def _serialize_snapshot_id(self, value: int | None) -> str | None:
        """Serialize int64 snapshot_id as string to avoid JS precision loss."""
        return None if value is None else str(value)


class LakeBatchListResponse(BaseModel):
    """Response for GET /api/v0/datalake/batches."""

    items: list[LakeBatchRead]
    next_cursor: str | None = None

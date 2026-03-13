# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake batch API schemas."""

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field


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
    storage_provider: str
    dataset_type: str
    storage_key: str
    row_count: int
    created_at: datetime
    application_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    content_type: str | None = None
    metadata_json: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True)

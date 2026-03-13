# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact API schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    'AccessArtifactCreate',
    'AccessArtifactRead',
]


class AccessArtifactCreate(BaseModel):
    """Internal schema for creating an access artifact. NOT exposed via REST."""

    application_id: uuid.UUID
    source_kind: str = Field(..., min_length=1, max_length=255)
    external_id: str = Field(..., min_length=1, max_length=255)
    payload: dict[str, Any]
    ingest_batch_id: str | None = Field(None, max_length=255)


class AccessArtifactRead(BaseModel):
    """Response schema for access artifact endpoints."""

    id: uuid.UUID
    application_id: uuid.UUID
    source_kind: str
    external_id: str
    payload: dict[str, Any]
    ingested_at: datetime
    ingest_batch_id: str | None

    model_config = ConfigDict(from_attributes=True)

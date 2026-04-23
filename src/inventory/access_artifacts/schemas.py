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
    artifact_type: str = Field(..., min_length=1, max_length=255)
    external_id: str = Field(..., min_length=1, max_length=255)
    payload: dict[str, Any]
    raw_name: str | None = Field(None, max_length=255)
    effect: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    ingest_batch_id: str | None = Field(None, max_length=255)
    observed_at: datetime | None = None


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

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding API schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict

__all__ = [
    'ArtifactBindingCreate',
    'ArtifactBindingRead',
]


class ArtifactBindingCreate(BaseModel):
    """Internal schema for creating an artifact binding. NOT exposed via REST."""

    artifact_id: uuid.UUID
    access_fact_id: uuid.UUID | None = None
    resource_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None


class ArtifactBindingRead(BaseModel):
    """Response schema for artifact binding endpoints."""

    id: uuid.UUID
    artifact_id: uuid.UUID
    access_fact_id: uuid.UUID | None
    resource_id: uuid.UUID | None
    account_id: uuid.UUID | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

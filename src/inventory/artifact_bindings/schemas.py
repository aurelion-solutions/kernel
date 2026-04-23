# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding API schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, field_validator

__all__ = [
    'SUPPORTED_TARGET_TYPES',
    'ArtifactBindingCreate',
    'ArtifactBindingRead',
]

# Single source of truth for supported target types.
# Validation occurs in the service layer; this constant is shared.
SUPPORTED_TARGET_TYPES: frozenset[str] = frozenset({'access_fact', 'resource', 'account', 'subject'})


class ArtifactBindingCreate(BaseModel):
    """Internal schema for creating an artifact binding. NOT exposed via REST."""

    artifact_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID

    @field_validator('target_type')
    @classmethod
    def validate_target_type(cls, v: str) -> str:
        if v not in SUPPORTED_TARGET_TYPES:
            raise ValueError(f'Unsupported target_type {v!r}. Supported: {sorted(SUPPORTED_TARGET_TYPES)}')
        return v


class ArtifactBindingRead(BaseModel):
    """Response schema for artifact binding endpoints."""

    id: uuid.UUID
    artifact_id: uuid.UUID
    target_type: str
    target_id: uuid.UUID
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

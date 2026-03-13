# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Initiative API schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, field_validator
from src.inventory.initiatives.models import InitiativeType

__all__ = [
    'InitiativeType',
    'InitiativeCreate',
    'InitiativePatch',
    'InitiativeRead',
]


class InitiativeCreate(BaseModel):
    """Request schema for creating an initiative."""

    access_fact_id: uuid.UUID
    type: InitiativeType
    origin: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    @field_validator('origin')
    @classmethod
    def origin_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError('origin must not be empty')
        return v


class InitiativePatch(BaseModel):
    """Partial update schema. type and access_fact_id are immutable."""

    origin: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class InitiativeRead(BaseModel):
    """Response schema for initiative endpoints."""

    id: uuid.UUID
    access_fact_id: uuid.UUID
    type: InitiativeType
    origin: str
    valid_from: datetime
    valid_until: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

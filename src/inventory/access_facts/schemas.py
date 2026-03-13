# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact API schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict
from src.inventory.access_facts.models import AccessFactEffect
from src.inventory.enums import Action

__all__ = [
    'AccessFactCreate',
    'AccessFactRead',
    'AccessFactEffect',
    'Action',
]


class AccessFactCreate(BaseModel):
    """Internal schema for creating an access fact. NOT exposed via REST."""

    subject_id: uuid.UUID
    account_id: uuid.UUID | None = None
    resource_id: uuid.UUID
    action: Action
    effect: AccessFactEffect
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class AccessFactRead(BaseModel):
    """Response schema for access fact endpoints."""

    id: uuid.UUID
    subject_id: uuid.UUID
    account_id: uuid.UUID | None
    resource_id: uuid.UUID
    action: Action
    effect: AccessFactEffect
    valid_from: datetime
    valid_until: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

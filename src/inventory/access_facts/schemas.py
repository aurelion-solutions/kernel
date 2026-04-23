# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact API schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict
from src.inventory.access_facts.models import AccessFactEffect

__all__ = [
    'AccessFactCreate',
    'AccessFactRead',
    'AccessFactEffect',
]


class AccessFactCreate(BaseModel):
    """Internal schema for creating an access fact. NOT exposed via REST.

    action_slug is resolved to action_id by the service layer. Handlers and
    internal callers identify actions by slug (human-friendly, stable across
    reseeds) — not by FK id.
    """

    subject_id: uuid.UUID
    account_id: uuid.UUID | None = None
    resource_id: uuid.UUID
    action_slug: str
    effect: AccessFactEffect
    # caller-supplied — no default (TASK.md Q8: server-side default would hide source-time signal)
    observed_at: datetime
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class AccessFactRead(BaseModel):
    """Response schema for access fact endpoints."""

    id: uuid.UUID
    subject_id: uuid.UUID
    account_id: uuid.UUID | None
    resource_id: uuid.UUID
    action_slug: str
    effect: AccessFactEffect
    is_active: bool
    revoked_at: datetime | None
    observed_at: datetime
    valid_from: datetime
    valid_until: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

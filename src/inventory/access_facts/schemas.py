# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact API schemas.

Phase 15 Step 16: AccessFactEffect moved here from deleted models.py.
AccessFactView added as the service-return DTO for lake-read paths.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
import uuid

from pydantic import BaseModel, ConfigDict

__all__ = [
    'AccessFactCreate',
    'AccessFactArtifactRefRead',
    'AccessFactRead',
    'AccessFactView',
    'AccessFactEffect',
]


class AccessFactEffect(StrEnum):
    """Effect of an access fact: allow or deny.

    Moved from inventory/access_facts/models.py (Phase 15 Step 16 — PG table dropped).
    """

    allow = 'allow'
    deny = 'deny'


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


class AccessFactArtifactRefRead(BaseModel):
    """Response schema for GET /access-facts/{fact_id}/artifact-ref.

    Resolves the chain: access_fact → reconciliation_delta_item → access_artifact.
    """

    artifact_id: uuid.UUID
    application_id: uuid.UUID
    external_id: str


class AccessFactView(BaseModel):
    """Frozen Pydantic v2 DTO for access fact reads — service-return type.

    Returned by AccessFactService.get_fact, list_facts, get_fact_by_natural_key.
    action_slug is resolved via DuckDB ref_actions_local join.
    """

    model_config = ConfigDict(frozen=True, strict=True, extra='forbid')

    id: uuid.UUID
    subject_id: uuid.UUID
    account_id: uuid.UUID | None
    resource_id: uuid.UUID
    action_id: int
    action_slug: str
    effect: AccessFactEffect
    valid_from: datetime
    valid_until: datetime | None
    is_active: bool
    revoked_at: datetime | None
    observed_at: datetime
    created_at: datetime

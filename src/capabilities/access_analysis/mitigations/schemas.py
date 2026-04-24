# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Mitigation Pydantic v2 schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, field_validator
from src.capabilities.access_analysis.mitigations.models import MitigationStatus


class MitigationCreate(BaseModel):
    """Payload for creating a new Mitigation."""

    model_config = ConfigDict(extra='forbid')

    rule_id: int
    control_id: int
    subject_id: uuid.UUID
    scope_key_id: int | None = None
    scope_value: str | None = None
    reason: str | None = None
    status: MitigationStatus = MitigationStatus.proposed
    valid_from: datetime
    valid_until: datetime | None = None
    owner_id: uuid.UUID
    created_by: str | None = None

    @field_validator('scope_value', mode='before')
    @classmethod
    def _normalise_scope_value(cls, v: object) -> str | None:
        if v is None:
            return None
        if isinstance(v, str):
            stripped = v.strip().lower()
            if not stripped:
                return None
            return stripped
        return v  # type: ignore[return-value]


class MitigationRead(BaseModel):
    """Read schema returned from all mitigation endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    rule_id: int
    control_id: int
    subject_id: uuid.UUID
    scope_key_id: int | None
    scope_value: str | None
    reason: str | None
    status: MitigationStatus
    valid_from: datetime
    valid_until: datetime | None
    owner_id: uuid.UUID
    created_at: datetime
    created_by: str | None


class MitigationStatusPatch(BaseModel):
    """Payload for PATCH /mitigations/{id}/status.

    Only status transitions are accepted here.  ``reason`` is required when
    revoking (status='revoked') and forbidden otherwise.
    Generic field updates (reason alone, valid_until changes) are out of scope.
    """

    model_config = ConfigDict(extra='forbid')

    status: MitigationStatus
    reason: str | None = None


class MitigationRevokeBody(BaseModel):
    """Payload for POST /mitigations/{id}/revoke."""

    model_config = ConfigDict(extra='forbid')

    reason: str

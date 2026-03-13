# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ThreatFact API schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ThreatFactUpsert(BaseModel):
    """Request body for PUT /threat-facts/{subject_id}."""

    risk_score: float = Field(ge=0.0, le=1.0)
    active_indicators: list[str] = Field(default_factory=list)
    account_id: uuid.UUID | None = None
    last_login_at: datetime | None = None
    failed_auth_count: int = Field(default=0, ge=0)
    observed_at: datetime | None = None

    @field_validator('active_indicators')
    @classmethod
    def validate_active_indicators(cls, v: list[str]) -> list[str]:
        """Strip whitespace, reject empty strings, duplicates, over-long items, and excess count."""
        if len(v) > 128:
            raise ValueError('active_indicators must not contain more than 128 items')
        seen: dict[str, int] = {}
        result: list[str] = []
        for item in v:
            stripped = item.strip()
            if not stripped:
                raise ValueError('active_indicators must not contain empty strings')
            if len(stripped) > 255:
                raise ValueError('active_indicators items must not exceed 255 characters')
            if stripped in seen:
                raise ValueError('active_indicators must not contain duplicates')
            seen[stripped] = 1
            result.append(stripped)
        return result


class ThreatFactRead(BaseModel):
    """Response schema for threat fact endpoints."""

    id: uuid.UUID
    subject_id: uuid.UUID
    account_id: uuid.UUID | None
    risk_score: float
    active_indicators: list[str]
    last_login_at: datetime | None
    failed_auth_count: int
    observed_at: datetime
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

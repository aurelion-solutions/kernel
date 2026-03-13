# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessUsageFact API schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AccessUsageFactCreate(BaseModel):
    """Request body for POST /access-usage-facts."""

    access_fact_id: uuid.UUID
    last_seen: datetime
    usage_count: int = Field(default=0, ge=0)
    window_from: datetime
    window_to: datetime | None = None

    @model_validator(mode='after')
    def validate_window_ordering(self) -> AccessUsageFactCreate:
        """window_to must be strictly greater than window_from when provided."""
        if self.window_to is not None and self.window_to <= self.window_from:
            raise ValueError('window_to must be strictly greater than window_from')
        return self


class AccessUsageFactRead(BaseModel):
    """Response schema for access usage fact endpoints."""

    id: uuid.UUID
    access_fact_id: uuid.UUID
    last_seen: datetime
    usage_count: int
    window_from: datetime
    window_to: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

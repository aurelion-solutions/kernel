# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Account schemas for reconciliation."""

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field
from src.inventory.accounts.models import AccountStatus

__all__ = [
    'AccountDTO',
    'AccountStatus',
    'AccountRead',
    'AccountPatch',
    'AccountBulkItem',
    'AccountBulkRequest',
    'AccountBulkResponse',
]


class AccountDTO(BaseModel):
    """Validated account payload from connector. identifier is the reconciliation key."""

    identifier: str = Field(..., min_length=1, description='Unique identifier from connector')
    username: str | None = None
    display_name: str | None = None
    email: str | None = None
    is_active: bool = True
    is_privileged: bool = False
    mfa_enabled: bool = False
    status: AccountStatus | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class AccountRead(BaseModel):
    """Schema for reading an Account via REST."""

    id: uuid.UUID
    application_id: uuid.UUID
    username: str
    display_name: str | None
    email: str | None
    is_active: bool
    is_privileged: bool
    mfa_enabled: bool
    status: AccountStatus
    subject_id: uuid.UUID | None
    meta: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    # Enriched display fields — populated by route handler (batch lookup, not N+1).
    application_code: str | None = None
    application_name: str | None = None
    subject_display: str | None = None

    model_config = ConfigDict(from_attributes=True)


class AccountPatch(BaseModel):
    """Schema for partially updating an Account."""

    status: AccountStatus | None = None
    subject_id: uuid.UUID | None = None


class AccountBulkItem(BaseModel):
    """Single item in a bulk upsert request."""

    application_id: uuid.UUID
    username: str = Field(..., min_length=1, max_length=255)
    external_id: str | None = Field(default=None, max_length=255)
    display_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, max_length=255)
    status: AccountStatus | None = None
    is_privileged: bool | None = None
    mfa_enabled: bool | None = None
    meta: dict[str, Any] | None = None


class AccountBulkRequest(BaseModel):
    """Request body for bulk account upsert."""

    items: list[AccountBulkItem] = Field(..., min_length=1, max_length=10_000)
    correlation_id: str | None = None


class AccountBulkResponse(BaseModel):
    """Response for bulk account upsert (lake-first path)."""

    row_count: int
    snapshot_id: int | None = None

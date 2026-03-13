# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Customer API schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field
from src.inventory.customers.models import CustomerPlanTier, CustomerTenantRole

__all__ = [
    'CustomerPlanTier',
    'CustomerTenantRole',
    'CustomerCreate',
    'CustomerRead',
    'CustomerPatch',
    'CustomerAttributeCreate',
    'CustomerAttributeRead',
]


class CustomerCreate(BaseModel):
    """Request body for POST /customers."""

    external_id: str = Field(..., min_length=1, max_length=255)
    email_verified: bool = False
    tenant_id: str | None = Field(None, max_length=255)
    tenant_role: CustomerTenantRole | None = None
    plan_tier: CustomerPlanTier | None = None
    mfa_enabled: bool = True
    is_locked: bool = False
    description: str | None = Field(None, max_length=255)


class CustomerRead(BaseModel):
    """Response for customer endpoints."""

    id: uuid.UUID
    external_id: str
    email_verified: bool
    tenant_id: str | None
    tenant_role: CustomerTenantRole | None
    plan_tier: CustomerPlanTier | None
    mfa_enabled: bool
    is_locked: bool
    description: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CustomerPatch(BaseModel):
    """Request body for PATCH /customers/{id}. Exactly four patchable fields."""

    email_verified: bool | None = None
    mfa_enabled: bool | None = None
    is_locked: bool | None = None
    plan_tier: CustomerPlanTier | None = None


class CustomerAttributeCreate(BaseModel):
    """Request body for POST /customers/{id}/attributes."""

    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=1024)


class CustomerAttributeRead(BaseModel):
    """Response for customer attribute endpoints."""

    id: uuid.UUID
    customer_id: uuid.UUID
    key: str
    value: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

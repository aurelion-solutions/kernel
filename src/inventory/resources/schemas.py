# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Resource API schemas. Enums re-exported from models (single source of truth)."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field
from src.inventory.resources.models import (
    ResourceDataSensitivity,
    ResourceEnvironment,
    ResourcePrivilegeLevel,
)

__all__ = [
    'ResourcePrivilegeLevel',
    'ResourceEnvironment',
    'ResourceDataSensitivity',
    'ResourceCreate',
    'ResourceRead',
    'ResourcePatch',
    'ResourceAttributeCreate',
    'ResourceAttributeRead',
]


class ResourceCreate(BaseModel):
    """Request body for POST /resources."""

    external_id: str = Field(..., min_length=1, max_length=255)
    application_id: uuid.UUID
    kind: str = Field(..., min_length=1, max_length=255)
    resource_type: str | None = Field(None, min_length=1, max_length=255)
    resource_key: str | None = Field(None, min_length=1, max_length=1024)
    parent_id: uuid.UUID | None = None
    path: str | None = Field(None, max_length=1024)
    description: str | None = Field(None, max_length=1024)
    privilege_level: ResourcePrivilegeLevel | None = None
    environment: ResourceEnvironment | None = None
    data_sensitivity: ResourceDataSensitivity | None = None


class ResourceRead(BaseModel):
    """Response schema for resource endpoints."""

    id: uuid.UUID
    external_id: str
    application_id: uuid.UUID
    kind: str
    resource_type: str
    resource_key: str
    parent_id: uuid.UUID | None
    path: str | None
    description: str | None
    privilege_level: ResourcePrivilegeLevel | None
    environment: ResourceEnvironment | None
    data_sensitivity: ResourceDataSensitivity | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ResourcePatch(BaseModel):
    """Request body for PATCH /resources/{id}. Uses model_fields_set for partial updates."""

    kind: str | None = Field(None, min_length=1, max_length=255)
    resource_type: str | None = Field(None, min_length=1, max_length=255)
    resource_key: str | None = Field(None, min_length=1, max_length=1024)
    parent_id: uuid.UUID | None = None
    path: str | None = None
    description: str | None = None
    privilege_level: ResourcePrivilegeLevel | None = None
    environment: ResourceEnvironment | None = None
    data_sensitivity: ResourceDataSensitivity | None = None

    model_config = ConfigDict(extra='forbid')


class ResourceAttributeCreate(BaseModel):
    """Request body for POST /resources/{id}/attributes."""

    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=1024)


class ResourceAttributeRead(BaseModel):
    """Response for resource attribute endpoints."""

    id: uuid.UUID
    resource_id: uuid.UUID
    key: str
    value: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

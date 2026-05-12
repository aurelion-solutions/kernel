# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""NHI API schemas."""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class NHICreate(BaseModel):
    """Request body for POST /nhi."""

    external_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=255)
    kind: str = Field(..., min_length=1, max_length=64)
    description: str | None = Field(None, max_length=255)
    is_locked: bool = False
    owner_employee_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None


class NHIRead(BaseModel):
    """Response for NHI endpoints."""

    id: uuid.UUID
    external_id: str
    name: str
    kind: str
    description: str | None
    is_locked: bool
    owner_employee_id: uuid.UUID | None
    application_id: uuid.UUID | None

    model_config = ConfigDict(from_attributes=True)


class NHIPatch(BaseModel):
    """Request body for PATCH /nhi/{id}."""

    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = Field(None, max_length=255)
    is_locked: bool | None = None
    owner_employee_id: uuid.UUID | None = None
    application_id: uuid.UUID | None = None
    attributes: dict[str, str] | None = None


class NHIAttributeCreate(BaseModel):
    """Request body for POST /nhi/{id}/attributes."""

    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=1024)


class NHIAttributeRead(BaseModel):
    """Response for NHI attribute endpoints."""

    id: uuid.UUID
    nhi_id: uuid.UUID
    key: str
    value: str

    model_config = ConfigDict(from_attributes=True)

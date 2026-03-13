# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Employee API schemas."""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class EmployeeCreate(BaseModel):
    """Request body for POST /employees."""

    person_id: uuid.UUID
    is_locked: bool = False
    description: str | None = Field(None, max_length=255)


class EmployeeRead(BaseModel):
    """Response for employee endpoints."""

    id: uuid.UUID
    person_id: uuid.UUID
    is_locked: bool
    description: str | None

    model_config = ConfigDict(from_attributes=True)


class EmployeeAttributeCreate(BaseModel):
    """Request body for POST /employees/{id}/attributes."""

    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=1024)


class EmployeeAttributeRead(BaseModel):
    """Response for employee attribute endpoints."""

    id: uuid.UUID
    employee_id: uuid.UUID
    key: str
    value: str

    model_config = ConfigDict(from_attributes=True)

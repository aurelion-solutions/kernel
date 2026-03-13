# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EmployeeRecord API schemas."""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class EmployeeRecordCreate(BaseModel):
    """Request body for POST /employee-records."""

    external_id: str = Field(..., min_length=1, max_length=255)
    application_id: uuid.UUID
    description: str | None = Field(None, max_length=255)


class EmployeeRecordRead(BaseModel):
    """Response for employee record endpoints."""

    id: uuid.UUID
    external_id: str
    application_id: uuid.UUID
    description: str | None

    model_config = ConfigDict(from_attributes=True)


class EmployeeRecordAttributeCreate(BaseModel):
    """Request body for POST /employee-records/{id}/attributes."""

    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=1024)


class EmployeeRecordAttributeRead(BaseModel):
    """Response for employee record attribute endpoints."""

    id: uuid.UUID
    employee_record_id: uuid.UUID
    key: str
    value: str

    model_config = ConfigDict(from_attributes=True)

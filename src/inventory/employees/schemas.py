# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Employee API schemas."""

import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EmployeeCreate(BaseModel):
    """Request body for POST /employees."""

    person_id: uuid.UUID
    is_locked: bool = False
    description: str | None = Field(None, max_length=255)
    org_unit_id: uuid.UUID | None = None


class EmployeeRead(BaseModel):
    """Response for employee endpoints."""

    id: uuid.UUID
    person_id: uuid.UUID
    is_locked: bool
    description: str | None
    org_unit_id: uuid.UUID | None

    model_config = ConfigDict(from_attributes=True)


class EmployeeListItem(BaseModel):
    """Single item in the employee list response."""

    id: uuid.UUID
    person_id: uuid.UUID
    is_locked: bool
    description: str | None
    org_unit_id: uuid.UUID | None

    model_config = ConfigDict(from_attributes=True)


class EmployeeListResponse(BaseModel):
    """Response for GET /employees.

    ``total`` is the unfiltered row count.
    ``limit`` and ``offset`` echo the validated params used for this page.
    """

    items: list[EmployeeListItem]
    total: int
    limit: int
    offset: int


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


class EmployeeBulkItem(BaseModel):
    """Single item in a bulk-upsert request."""

    person_external_id: str = Field(..., min_length=1, max_length=255)
    is_locked: bool = Field(default=False)
    description: str | None = Field(default=None, max_length=255)
    org_unit_external_id: str | None = Field(default=None, max_length=255)
    attributes: dict[str, str] | None = Field(default=None)


class EmployeeBulkRequest(BaseModel):
    """Request body for POST /employees/bulk."""

    items: list[EmployeeBulkItem] = Field(..., min_length=1, max_length=500)

    @model_validator(mode='after')
    def _check_unique_person_external_ids(self) -> 'EmployeeBulkRequest':
        seen: set[str] = set()
        for item in self.items:
            if item.person_external_id in seen:
                raise ValueError(f'Duplicate person_external_id in request: {item.person_external_id}')
            seen.add(item.person_external_id)
        return self


class EmployeeBulkResponse(BaseModel):
    """Response for POST /employees/bulk (lake-first path)."""

    row_count: int
    snapshot_id: int | None
    backend: str = 'iceberg'


class EmployeePatch(BaseModel):
    """Request body for PATCH /employees/{id}.

    All fields are optional — only set fields are applied. Any change to any
    field (org_unit_id, description, attributes) emits a single
    ``inventory.employee.updated`` event carrying a ``changes`` map.
    """

    org_unit_id: uuid.UUID | None = Field(default=None)
    description: str | None = Field(default=None, max_length=255)
    attributes: dict[str, str] | None = Field(default=None)

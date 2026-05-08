# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Person API schemas."""

import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PersonCreate(BaseModel):
    """Request body for POST /persons."""

    external_id: str = Field(..., min_length=1, max_length=255)
    full_name: str = Field(..., min_length=1, max_length=255)


class PersonRead(BaseModel):
    """Response for person endpoints."""

    id: uuid.UUID
    external_id: str
    full_name: str

    model_config = ConfigDict(from_attributes=True)


class PersonAttributeCreate(BaseModel):
    """Request body for POST /persons/{id}/attributes."""

    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=1024)


class PersonAttributeRead(BaseModel):
    """Response for person attribute endpoints."""

    id: uuid.UUID
    person_id: uuid.UUID
    key: str
    value: str

    model_config = ConfigDict(from_attributes=True)


class PersonBulkItem(BaseModel):
    """Single item in a bulk-upsert request."""

    external_id: str = Field(..., min_length=1, max_length=255)
    full_name: str = Field(..., min_length=1, max_length=255)


class PersonBulkRequest(BaseModel):
    """Request body for POST /persons/bulk."""

    items: list[PersonBulkItem] = Field(..., min_length=1, max_length=500)

    @model_validator(mode='after')
    def _check_unique_external_ids(self) -> 'PersonBulkRequest':
        seen: set[str] = set()
        for item in self.items:
            if item.external_id in seen:
                raise ValueError(f'Duplicate external_id in request: {item.external_id!r}')
            seen.add(item.external_id)
        return self


class PersonBulkResponse(BaseModel):
    """Response for POST /persons/bulk (lake-first path)."""

    row_count: int
    snapshot_id: int | None
    backend: str = 'iceberg'

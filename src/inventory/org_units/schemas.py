# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OrgUnit API schemas."""

import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OrgUnitRead(BaseModel):
    """Response schema for an org unit."""

    id: uuid.UUID
    external_id: str
    name: str
    parent_id: uuid.UUID | None

    model_config = ConfigDict(from_attributes=True)


class OrgUnitBulkItem(BaseModel):
    """Single item in a bulk-upsert request."""

    external_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=255)
    parent_external_id: str | None = Field(default=None, max_length=255)

    @model_validator(mode='after')
    def _check_no_self_reference(self) -> 'OrgUnitBulkItem':
        if self.parent_external_id is not None and self.parent_external_id == self.external_id:
            raise ValueError(f'parent_external_id cannot reference itself: {self.external_id!r}')
        return self


class OrgUnitBulkRequest(BaseModel):
    """Request body for POST /org-units/bulk."""

    items: list[OrgUnitBulkItem] = Field(..., min_length=1, max_length=500)

    @model_validator(mode='after')
    def _check_unique_external_ids(self) -> 'OrgUnitBulkRequest':
        seen: set[str] = set()
        for item in self.items:
            if item.external_id in seen:
                raise ValueError(f'Duplicate external_id in request: {item.external_id!r}')
            seen.add(item.external_id)
        return self


class OrgUnitBulkResponse(BaseModel):
    """Response for POST /org-units/bulk (lake-first path)."""

    row_count: int
    snapshot_id: int | None
    backend: str = 'iceberg'


class OrgUnitListItem(BaseModel):
    """Single item in the org-units list response."""

    id: uuid.UUID
    external_id: str
    name: str

    model_config = ConfigDict(from_attributes=True)


class OrgUnitListResponse(BaseModel):
    """Response for GET /org-units."""

    items: list[OrgUnitListItem]

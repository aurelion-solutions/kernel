# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OrgUnit API schemas."""

from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OrgUnitRead(BaseModel):
    """Response schema for an org unit."""

    id: uuid.UUID
    external_id: str
    name: str
    description: str | None
    parent_id: uuid.UUID | None
    is_internal: bool

    model_config = ConfigDict(from_attributes=True)


class OrgUnitCreate(BaseModel):
    """Request body for POST /org-units (single external org-unit creation)."""

    external_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    is_internal: Literal[False]
    parent_id: uuid.UUID | None = None

    model_config = ConfigDict(extra='forbid')


class OrgUnitUpdate(BaseModel):
    """Request body for PUT /org-units/{id}.

    Only name and description are editable. external_id, is_internal, and
    parent_id are not accepted — unknown fields trigger 422 via extra='forbid'.
    """

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None

    model_config = ConfigDict(extra='forbid')


class OrgUnitBulkItem(BaseModel):
    """Single item in a bulk-upsert request."""

    external_id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=255)
    parent_external_id: str | None = Field(default=None, max_length=255)
    is_internal: bool = True

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
    parent_id: uuid.UUID | None
    description: str | None
    is_internal: bool

    model_config = ConfigDict(from_attributes=True)


class OrgUnitListResponse(BaseModel):
    """Response for GET /org-units.

    ``total`` is the count of all matching rows (unfiltered when no filter
    is active; if a filter is added later, total reflects filtered count).
    ``limit`` and ``offset`` echo the validated params used for this page.
    """

    items: list[OrgUnitListItem]
    total: int
    limit: int
    offset: int

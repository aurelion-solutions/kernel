# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Subject API schemas. Enums re-exported from models (single source of truth)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.inventory.subjects.models import (
    SubjectCustomerStatus,
    SubjectEmployeeStatus,
    SubjectKind,
    SubjectNHIKind,
    SubjectNHIStatus,
    SubjectStatus,
)

__all__ = [
    'SubjectKind',
    'SubjectNHIKind',
    'SubjectEmployeeStatus',
    'SubjectNHIStatus',
    'SubjectCustomerStatus',
    'SubjectStatus',
    'SubjectCreate',
    'SubjectRead',
    'SubjectPatch',
    'SubjectAttributeCreate',
    'SubjectAttributeRead',
    'SubjectBulkItem',
    'SubjectBulkRequest',
    'SubjectBulkResponse',
]

_EMPLOYEE_STATUSES = frozenset(v.value for v in SubjectEmployeeStatus)
_NHI_STATUSES = frozenset(v.value for v in SubjectNHIStatus)
_CUSTOMER_STATUSES = frozenset(v.value for v in SubjectCustomerStatus)

_STATUS_VOCAB: dict[str, frozenset[str]] = {
    SubjectKind.employee: _EMPLOYEE_STATUSES,
    SubjectKind.nhi: _NHI_STATUSES,
    SubjectKind.customer: _CUSTOMER_STATUSES,
}


def _check_status_for_kind(kind: SubjectKind, status: str) -> None:
    allowed = _STATUS_VOCAB[kind]
    if status not in allowed:
        raise ValueError(f"status '{status}' is not valid for kind '{kind}'. Allowed: {sorted(allowed)}")


class SubjectCreate(BaseModel):
    """Request body for POST /subjects."""

    external_id: Annotated[str, Field(min_length=1, max_length=255)]
    kind: SubjectKind
    nhi_kind: SubjectNHIKind | None = None
    principal_employee_id: uuid.UUID | None = None
    principal_nhi_id: uuid.UUID | None = None
    principal_customer_id: uuid.UUID | None = None
    status: SubjectStatus

    @model_validator(mode='after')
    def _validate_consistency(self) -> SubjectCreate:
        # kind ↔ nhi_kind
        if self.kind == SubjectKind.nhi and self.nhi_kind is None:
            raise ValueError("nhi_kind is required when kind is 'nhi'")
        if self.kind != SubjectKind.nhi and self.nhi_kind is not None:
            raise ValueError("nhi_kind must be null when kind is not 'nhi'")

        # kind ↔ principal exclusivity
        non_null = sum(
            [
                self.principal_employee_id is not None,
                self.principal_nhi_id is not None,
                self.principal_customer_id is not None,
            ]
        )
        if non_null != 1:
            raise ValueError(
                'Exactly one of principal_employee_id / principal_nhi_id / principal_customer_id must be provided'
            )

        if self.kind == SubjectKind.employee and self.principal_employee_id is None:
            raise ValueError("principal_employee_id must be set when kind is 'employee'")
        if self.kind == SubjectKind.nhi and self.principal_nhi_id is None:
            raise ValueError("principal_nhi_id must be set when kind is 'nhi'")
        if self.kind == SubjectKind.customer and self.principal_customer_id is None:
            raise ValueError("principal_customer_id must be set when kind is 'customer'")

        # kind ↔ status vocabulary
        _check_status_for_kind(self.kind, self.status)

        return self


class SubjectRead(BaseModel):
    """Response schema for subject endpoints."""

    id: uuid.UUID
    external_id: str
    kind: SubjectKind
    nhi_kind: SubjectNHIKind | None
    principal_employee_id: uuid.UUID | None
    principal_nhi_id: uuid.UUID | None
    principal_customer_id: uuid.UUID | None
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SubjectPatch(BaseModel):
    """Request body for PATCH /subjects/{id}. Exactly one patchable field: status."""

    status: SubjectStatus | None = None


class SubjectAttributeCreate(BaseModel):
    """Request body for POST /subjects/{id}/attributes."""

    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=1024)


class SubjectAttributeRead(BaseModel):
    """Response for subject attribute endpoints."""

    id: uuid.UUID
    subject_id: uuid.UUID
    key: str
    value: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SubjectBulkItem(BaseModel):
    """Single item in a bulk-upsert request. Employee subjects only."""

    external_id: Annotated[str, Field(min_length=1, max_length=255)]
    person_external_id: Annotated[str, Field(min_length=1, max_length=255)]
    status: SubjectEmployeeStatus = SubjectEmployeeStatus.active

    # kind is fixed at 'employee' for this step — encode as a Literal so
    # callers cannot accidentally send 'nhi' / 'customer' and get a
    # confusing 500 from the resolver.
    kind: Literal['employee'] = 'employee'


class SubjectBulkRequest(BaseModel):
    """Request body for POST /subjects/bulk."""

    items: list[SubjectBulkItem] = Field(..., min_length=1, max_length=500)

    @model_validator(mode='after')
    def _check_unique_business_keys(self) -> SubjectBulkRequest:
        seen: set[tuple[str, str]] = set()
        for item in self.items:
            key = (item.kind, item.external_id)
            if key in seen:
                raise ValueError(f'Duplicate (kind, external_id) in request: {item.kind}/{item.external_id}')
            seen.add(key)
        return self


class SubjectBulkResponse(BaseModel):
    """Response for POST /subjects/bulk."""

    upserted: int
    ids: list[uuid.UUID]

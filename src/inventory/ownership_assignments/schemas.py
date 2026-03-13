# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OwnershipAssignment API schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, model_validator
from src.inventory.ownership_assignments.models import OwnershipKind

__all__ = [
    'OwnershipKind',
    'OwnershipAssignmentCreate',
    'OwnershipAssignmentRead',
]


class OwnershipAssignmentCreate(BaseModel):
    """Request body for POST /ownership-assignments."""

    subject_id: uuid.UUID
    resource_id: uuid.UUID | None = None
    account_id: uuid.UUID | None = None
    kind: OwnershipKind

    @model_validator(mode='after')
    def validate_xor_target(self) -> OwnershipAssignmentCreate:
        """Exactly one of resource_id or account_id must be set."""
        if (self.resource_id is None) == (self.account_id is None):
            raise ValueError('Exactly one of resource_id or account_id must be provided')
        return self


class OwnershipAssignmentRead(BaseModel):
    """Response schema for ownership assignment endpoints."""

    id: uuid.UUID
    subject_id: uuid.UUID
    resource_id: uuid.UUID | None
    account_id: uuid.UUID | None
    kind: OwnershipKind
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

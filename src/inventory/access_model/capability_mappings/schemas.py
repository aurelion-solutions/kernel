# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityMapping Pydantic schemas — validation and serialization."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

# ---------------------------------------------------------------------------
# Constrained string types
# ---------------------------------------------------------------------------

ScopeAttrKey = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r'^[a-zA-Z][a-zA-Z0-9_]*$', strip_whitespace=True),
]
ScopeConstantValue = Annotated[
    str,
    StringConstraints(min_length=1, max_length=255, strip_whitespace=True),
]
ResourceKindStr = Annotated[str, StringConstraints(min_length=1, max_length=128, strip_whitespace=True)]
ResourcePathGlobStr = Annotated[str, StringConstraints(min_length=1, max_length=512, strip_whitespace=True)]
ActionSlugStr = Annotated[
    str,
    StringConstraints(min_length=1, max_length=64, pattern=r'^[a-z0-9_]+$', strip_whitespace=True),
]

# ---------------------------------------------------------------------------
# ScopeValueSource discriminated union — four kinds, closed set
# ---------------------------------------------------------------------------


class _SubjectAttributeSource(BaseModel):
    kind: Literal['subject_attribute']
    key: ScopeAttrKey


class _ResourceAttributeSource(BaseModel):
    kind: Literal['resource_attribute']
    key: ScopeAttrKey


class _ApplicationIdSource(BaseModel):
    kind: Literal['application_id']


class _ConstantSource(BaseModel):
    kind: Literal['constant']
    value: ScopeConstantValue


ScopeValueSource = Annotated[
    _SubjectAttributeSource | _ResourceAttributeSource | _ApplicationIdSource | _ConstantSource,
    Field(discriminator='kind'),
]

# ---------------------------------------------------------------------------
# CapabilityMappingCreate
# ---------------------------------------------------------------------------


class CapabilityMappingCreate(BaseModel):
    capability_id: int
    application_id: UUID | None = None
    resource_id: UUID | None = None
    resource_kind: ResourceKindStr | None = None
    resource_path_glob: ResourcePathGlobStr | None = None
    action_slug: ActionSlugStr | None = None
    scope_key_id: int | None = None
    scope_value_source: ScopeValueSource
    is_active: bool = True
    created_by: Annotated[str | None, StringConstraints(max_length=255)] = None

    @model_validator(mode='after')
    def _check_resource_match_xor(self) -> CapabilityMappingCreate:
        count = sum(1 for v in (self.resource_id, self.resource_kind, self.resource_path_glob) if v is not None)
        if count != 1:
            raise ValueError('exactly one of resource_id, resource_kind, resource_path_glob must be set')
        return self


# ---------------------------------------------------------------------------
# CapabilityMappingRead
# ---------------------------------------------------------------------------


class CapabilityMappingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    capability_id: int
    application_id: UUID | None
    resource_id: UUID | None
    resource_kind: str | None
    resource_path_glob: str | None
    action_slug: str | None
    scope_key_id: int
    scope_value_source: ScopeValueSource
    is_active: bool
    created_at: datetime
    created_by: str | None


# ---------------------------------------------------------------------------
# CapabilityMappingPatch
# ---------------------------------------------------------------------------


class CapabilityMappingPatch(BaseModel):
    model_config = ConfigDict(extra='forbid')

    resource_id: UUID | None = None
    resource_kind: ResourceKindStr | None = None
    resource_path_glob: ResourcePathGlobStr | None = None
    action_slug: ActionSlugStr | None = None
    scope_key_id: int | None = None
    scope_value_source: ScopeValueSource | None = None
    is_active: bool | None = None

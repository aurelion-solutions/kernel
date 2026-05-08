# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Capability Pydantic v2 schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

# Lowercase snake_case slug: a-z, 0-9, underscore only.
CapabilitySlugStr = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=128,
        pattern=r'^[a-z0-9_]+$',
        strip_whitespace=True,
    ),
]

CapabilityNameStr = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=255,
        strip_whitespace=True,
    ),
]


class CapabilityCreate(BaseModel):
    slug: CapabilitySlugStr
    name: CapabilityNameStr
    description: str | None = None
    is_active: bool = True
    created_by: Annotated[str | None, StringConstraints(max_length=255)] = None


class CapabilityRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    created_by: str | None


class CapabilityPatch(BaseModel):
    # slug is deliberately excluded — slugs are immutable after creation (SoD rules reference them by slug)
    name: CapabilityNameStr | None = None
    description: str | None = None
    is_active: bool | None = None

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityScopeKey Pydantic v2 schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints

# Uppercase snake_case code: must start with a letter, then letters/digits/underscores.
CapabilityScopeKeyCodeStr = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r'^[A-Z][A-Z0-9_]*$',
        strip_whitespace=True,
    ),
]

CapabilityScopeKeyNameStr = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=255,
        strip_whitespace=True,
    ),
]


class CapabilityScopeKeyCreate(BaseModel):
    code: CapabilityScopeKeyCodeStr
    name: CapabilityScopeKeyNameStr
    description: str | None = None
    is_active: bool = True
    created_by: Annotated[str | None, StringConstraints(max_length=255)] = None


class CapabilityScopeKeyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str | None
    is_active: bool
    created_at: datetime
    created_by: str | None


class CapabilityScopeKeyPatch(BaseModel):
    # code is deliberately excluded — codes are immutable after creation
    # (mappings and rules reference them by id, but code is the human-stable identifier;
    # renaming it silently breaks operator scripts and seeded references)
    name: CapabilityScopeKeyNameStr | None = None
    description: str | None = None
    is_active: bool | None = None

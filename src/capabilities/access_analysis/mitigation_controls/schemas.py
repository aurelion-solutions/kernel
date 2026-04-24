# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""MitigationControl Pydantic v2 schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints
from src.capabilities.access_analysis.mitigation_controls.models import MitigationControlType

# SCREAMING_SNAKE_CASE: uppercase letters, digits, underscores; must start with a letter.
MitigationControlCodeStr = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r'^[A-Z][A-Z0-9_]*$',
        strip_whitespace=True,
    ),
]

MitigationControlNameStr = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=255,
        strip_whitespace=True,
    ),
]


class MitigationControlCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')

    code: MitigationControlCodeStr
    name: MitigationControlNameStr
    description: str | None = None
    type: MitigationControlType
    is_active: bool = True
    created_by: Annotated[str | None, StringConstraints(max_length=255)] = None


class MitigationControlRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str | None
    type: MitigationControlType
    is_active: bool
    created_at: datetime
    created_by: str | None


class MitigationControlPatch(BaseModel):
    # code is deliberately excluded — codes are immutable after creation
    model_config = ConfigDict(extra='forbid')

    name: MitigationControlNameStr | None = None
    description: str | None = None
    type: MitigationControlType | None = None
    is_active: bool | None = None

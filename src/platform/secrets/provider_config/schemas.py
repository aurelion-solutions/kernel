# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Provider API schemas."""

from typing import Any

from pydantic import BaseModel, Field


class ProviderCreate(BaseModel):
    """Request body for creating a provider."""

    name: str = Field(..., min_length=1, max_length=64, pattern=r'^[a-zA-Z0-9_-]+$')
    type: str = Field(..., min_length=1, max_length=64)
    config: dict[str, Any] = Field(default_factory=dict)


class ProviderRead(BaseModel):
    """Provider response schema."""

    id: str
    name: str
    type: str
    config: dict[str, Any]

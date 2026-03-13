# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Role schemas for reconciliation."""

from typing import Any

from pydantic import BaseModel, Field


class RoleDTO(BaseModel):
    """Validated role payload from connector. identifier is the reconciliation key."""

    identifier: str = Field(..., min_length=1, description='Unique identifier from connector')
    name: str | None = None
    display_name: str | None = None
    type: str | None = None
    is_active: bool = True
    meta: dict[str, Any] = Field(default_factory=dict)

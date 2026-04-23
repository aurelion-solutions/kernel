# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Action reference vocabulary — read DTOs."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ActionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    slug: str
    description: str | None
    created_at: datetime

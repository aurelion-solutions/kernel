# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from pydantic import BaseModel, Field


class AccountCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=255)
    email: str | None = Field(default=None, max_length=255)

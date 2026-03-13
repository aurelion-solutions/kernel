# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

CODE_PATTERN = r'^[a-z0-9][a-z0-9_-]{0,63}$'


class ApplicationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    code: str = Field(..., min_length=1, max_length=64, pattern=CODE_PATTERN)
    config: dict = Field(default_factory=dict)
    required_connector_tags: list[str] = Field(default_factory=list)
    is_active: bool = True


class ApplicationUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=255)
    code: str | None = Field(default=None, min_length=1, max_length=64, pattern=CODE_PATTERN)
    config: dict | None = None
    required_connector_tags: list[str] | None = None
    is_active: bool | None = None

    @model_validator(mode='after')
    def at_least_one_field(self) -> Self:
        if (
            self.name is None
            and self.code is None
            and self.config is None
            and self.required_connector_tags is None
            and self.is_active is None
        ):
            raise ValueError('At least one field must be provided for update')
        return self


class ApplicationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    code: str
    config: dict
    required_connector_tags: list[str]
    is_active: bool
    created_at: datetime
    updated_at: datetime

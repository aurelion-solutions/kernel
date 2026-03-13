# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Person API schemas."""

import uuid

from pydantic import BaseModel, ConfigDict, Field


class PersonCreate(BaseModel):
    """Request body for POST /persons."""

    external_id: str = Field(..., min_length=1, max_length=255)
    description: str = Field(..., min_length=1, max_length=255)


class PersonRead(BaseModel):
    """Response for person endpoints."""

    id: uuid.UUID
    external_id: str
    description: str

    model_config = ConfigDict(from_attributes=True)


class PersonAttributeCreate(BaseModel):
    """Request body for POST /persons/{id}/attributes."""

    key: str = Field(..., min_length=1, max_length=255)
    value: str = Field(..., min_length=1, max_length=1024)


class PersonAttributeRead(BaseModel):
    """Response for person attribute endpoints."""

    id: uuid.UUID
    person_id: uuid.UUID
    key: str
    value: str

    model_config = ConfigDict(from_attributes=True)

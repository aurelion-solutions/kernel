# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Secret API schemas."""

from pydantic import BaseModel, Field

_KEY_PATTERN = r'^[a-zA-Z0-9_-]+(/[a-zA-Z0-9_-]+)*$'
_NAMESPACE_PATTERN = r'^[a-zA-Z0-9_-]+$'


class SecretCreate(BaseModel):
    """Request body for creating a secret. value is write-only."""

    key: str = Field(..., min_length=1, max_length=255, pattern=_KEY_PATTERN)
    provider: str = Field(..., min_length=1, max_length=64)
    namespace: str = Field(..., min_length=1, max_length=255, pattern=_NAMESPACE_PATTERN)
    value: str = Field(..., min_length=1)


class SecretRead(BaseModel):
    """Secret metadata response. No value field."""

    key: str = Field(..., min_length=1, max_length=255, pattern=_KEY_PATTERN)
    provider: str = Field(..., min_length=1, max_length=64)
    namespace: str = Field(..., min_length=1, max_length=255, pattern=_NAMESPACE_PATTERN)


class SecretDelete(BaseModel):
    """Request body for deleting a secret. No value field."""

    key: str = Field(..., min_length=1, max_length=255, pattern=_KEY_PATTERN)
    provider: str = Field(..., min_length=1, max_length=64)
    namespace: str = Field(..., min_length=1, max_length=255, pattern=_NAMESPACE_PATTERN)

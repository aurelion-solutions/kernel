# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic v2 schemas for LLMModel, LLMExecutionProfile, and inference."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field
from src.platform.llm.models import LLMProvider


class LLMModelCreate(BaseModel):
    """Input schema for creating an LLMModel.

    Cross-field provider/credentials/path validation happens in the service
    layer (Step 8), not here, because it requires DB lookups (Secret existence)
    and filesystem checks (local_path readability) that are inappropriate for
    Pydantic schemas.
    """

    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, max_length=255)
    description: str | None = None
    provider: LLMProvider
    local_path: str | None = None
    endpoint_url: str | None = Field(default=None, max_length=2048)
    model_ref: str | None = Field(default=None, max_length=255)
    context_window: int | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=None, gt=0)
    default_params: dict[str, Any] = Field(default_factory=dict)
    secret_id: uuid.UUID | None = None
    is_active: bool = True


class LLMModelRead(BaseModel):
    """Output schema for reading an LLMModel."""

    model_config = ConfigDict(from_attributes=True, extra='forbid')

    id: uuid.UUID
    name: str
    description: str | None
    provider: LLMProvider
    local_path: str | None
    endpoint_url: str | None
    model_ref: str | None
    context_window: int | None
    max_total_tokens: int | None
    default_params: dict[str, Any]
    secret_id: uuid.UUID | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class LLMModelUpdate(BaseModel):
    """PATCH body for updating an LLMModel.

    PATCH semantics: a field *set* to ``null`` in the JSON body explicitly
    clears it on the row.  The service MUST iterate over
    ``request.model_fields_set`` (not ``model_dump(exclude_none=True)``)
    so that clearing nullable columns is possible.

    Provider is intentionally absent — changing the provider rewrites the
    wiring constraints between ``local_path``, ``endpoint_url``,
    ``model_ref``, and ``secret_id``; safer to delete and re-create.
    Any body containing ``provider`` will fail Pydantic validation
    (``extra='forbid'``).
    """

    model_config = ConfigDict(extra='forbid')

    name: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = None
    local_path: str | None = None
    endpoint_url: str | None = Field(default=None, max_length=2048)
    model_ref: str | None = Field(default=None, max_length=255)
    context_window: int | None = Field(default=None, gt=0)
    max_total_tokens: int | None = Field(default=None, gt=0)
    default_params: dict[str, Any] | None = None
    secret_id: uuid.UUID | None = None
    is_active: bool | None = None


class LLMExecutionProfileCreate(BaseModel):
    """Input schema for creating an LLMExecutionProfile.

    Per-key validation of `param_overrides` (allowed keys, bounded values)
    happens in the service layer in Step 9, because it depends on
    `LLMModel.default_params` shape and `LLMSettings`. Pydantic accepts any
    string-keyed dict here.
    """

    model_config = ConfigDict(extra='forbid')

    name: str = Field(min_length=1, max_length=255)
    model_id: uuid.UUID
    param_overrides: dict[str, Any] = Field(default_factory=dict)


class LLMExecutionProfileRead(BaseModel):
    """Output schema for reading an LLMExecutionProfile."""

    model_config = ConfigDict(from_attributes=True, extra='forbid')

    id: uuid.UUID
    name: str
    model_id: uuid.UUID
    param_overrides: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class LLMExecutionProfileUpdate(BaseModel):
    """PATCH body for updating an LLMExecutionProfile.

    ``model_id`` is intentionally absent — re-pointing a profile to a
    different model would invalidate ``param_overrides`` semantics against
    the new model's ``default_params``.  Any body containing ``model_id``
    will fail Pydantic validation (``extra='forbid'``) and return 422.

    PATCH semantics: only fields present in the request body are applied.
    Setting a field to ``null`` when the column is NOT NULL is rejected by
    ``_reject_profile_null_on_not_null_fields`` in the service before flush.
    """

    model_config = ConfigDict(extra='forbid')

    name: str | None = Field(default=None, min_length=1, max_length=255)
    param_overrides: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Inference schemas
# ---------------------------------------------------------------------------


class LLMMessageIn(BaseModel):
    """Pydantic wrapper for a single chat message passed to inference.

    Converts to a ``LLMMessage`` dataclass in the service layer.
    ``extra='forbid'`` ensures no stray fields slip through.
    """

    model_config = ConfigDict(extra='forbid')

    role: Literal['system', 'user', 'assistant']
    content: str


class InferenceRequest(BaseModel):
    """Request body for POST /api/v0/inference and /api/v0/inference/stream.

    Size-limit validation (max_messages, max_chars_per_message, max_total_chars)
    happens in the service layer using ``LLMSettings``, not here.
    """

    model_config = ConfigDict(extra='forbid')

    execution_profile_id: uuid.UUID
    messages: list[LLMMessageIn]


class InferenceResponse(BaseModel):
    """Response body for POST /api/v0/inference (JSON non-streaming path)."""

    model_config = ConfigDict(extra='forbid')

    output: str
    model_id: uuid.UUID
    execution_profile_id: uuid.UUID
    tokens_used: int
    latency_ms: float
    ttft_ms: float | None

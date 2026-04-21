# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Domain event envelope schema."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import re
from typing import Any, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_EVENT_TYPE_RE = re.compile(r'^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$')


class EventParticipantKind(str, Enum):
    """Participant classification for :class:`EventEnvelope` initiator / actor / target.

    Deliberately duplicated from ``LogParticipantKind`` — peer slice, no cross-slice import.
    """

    SYSTEM = 'system'
    USER = 'user'
    CONNECTOR = 'connector'
    CAPABILITY = 'capability'
    APPLICATION = 'application'


class EventEnvelope(BaseModel):
    """Immutable domain event envelope published to ``aurelion.events``.

    Routing key == ``event_type``, byte-for-byte.

    Participant triad (flat):

    - **initiator**: who wanted / started the action.
    - **actor**: who executes the current step.
    - **target**: what the action is performed on.

    All participant fields are optional — producers supply what they know.
    """

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    event_id: UUID
    event_type: str
    occurred_at: datetime
    correlation_id: str
    causation_id: UUID | None = Field(default=None)
    payload: dict[str, Any] = Field(default_factory=dict)
    initiator_kind: EventParticipantKind | None = None
    initiator_id: str | None = None
    actor_kind: EventParticipantKind | None = None
    actor_id: str | None = None
    target_kind: EventParticipantKind | None = None
    target_id: str | None = None
    schema_version: str = '1'

    @field_validator('event_type')
    @classmethod
    def _validate_event_type(cls, v: str) -> str:
        if not _EVENT_TYPE_RE.match(v):
            raise ValueError(
                'event_type must match <domain>.<entity>.<operation> with each segment matching [a-z0-9_]+'
            )
        return v

    @field_validator('occurred_at')
    @classmethod
    def _validate_occurred_at(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError('occurred_at must be timezone-aware (UTC)')
        return v

    @field_validator('correlation_id', mode='before')
    @classmethod
    def _validate_correlation_id(cls, v: object) -> str:
        if isinstance(v, UUID):
            return str(v)
        if isinstance(v, str):
            s = v.strip()
            if not s:
                raise ValueError('correlation_id must be a non-empty string')
            return s
        raise ValueError('correlation_id must be a non-empty string or UUID')

    @field_validator('initiator_id', 'actor_id', 'target_id')
    @classmethod
    def _validate_optional_id_strings(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError('participant id must be non-empty if set')
        return v

    @model_validator(mode='after')
    def _causation_not_self_referential(self) -> Self:
        if self.causation_id is not None and self.causation_id == self.event_id:
            raise ValueError('causation_id must not equal event_id')
        return self

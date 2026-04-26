# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Structured log event schema, levels, and propagation helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any, Self
import uuid
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from src.core.context import current_correlation_id


class LogLevel(str, Enum):
    """Allowed log levels for platform events."""

    DEBUG = 'debug'
    INFO = 'info'
    WARNING = 'warning'
    ERROR = 'error'
    CRITICAL = 'critical'


class LogParticipantKind(str, Enum):
    """Minimal participant classification for :class:`LogEvent` initiator/actor/target."""

    SYSTEM = 'system'
    USER = 'user'
    CONNECTOR = 'connector'
    CAPABILITY = 'capability'
    APPLICATION = 'application'


class LogEvent(BaseModel):
    """Unified structured log event for tracing and debug correlation.

    Semantics:

    - **initiator**: who wanted or started the action.
    - **actor**: who executes the current step.
    - **target**: what the action is performed on.

    **Propagation** (use :func:`new_root_log_event` and :func:`new_downstream_log_event`):

    - A **root** event generates a new ``event_id`` and ``correlation_id``, and sets
      ``causation_id`` to ``None``.
    - A **downstream** event generates a new ``event_id``, preserves the parent's
      ``correlation_id``, and sets ``causation_id`` to the parent's ``event_id``.

    ``causation_id`` is ``None`` only for trace roots; downstream events must set it
    to the parent event's ``event_id``.
    """

    model_config = ConfigDict(frozen=True, extra='forbid')

    event_id: UUID = Field(description='Unique id of this event.')
    # DEPRECATED: retained for legacy interop only; do not populate in new code.
    # LogService.emit_log / emit_safe no longer accept an event_type parameter.
    # Removal is scheduled for the dedicated legacy-migration phase after Phase 10 closes.
    event_type: str | None = None
    timestamp: datetime
    level: LogLevel
    message: str
    component: str
    correlation_id: str = Field(
        description='Stable id for the whole logical trace (opaque string); copied on downstream events.',
    )
    causation_id: UUID | None = Field(
        default=None,
        description='Immediate parent event_id; None only for trace roots.',
    )
    payload: dict[str, Any] = Field(default_factory=dict)
    initiator_type: LogParticipantKind
    initiator_id: str
    actor_type: LogParticipantKind
    actor_id: str
    target_type: LogParticipantKind
    target_id: str

    @field_validator('message', 'component')
    @classmethod
    def _non_empty_trimmed(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('must be a non-empty string')
        return v

    @field_validator('event_type')
    @classmethod
    def _event_type_non_empty_if_set(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not v.strip():
            raise ValueError('must be a non-empty string when provided')
        return v

    @field_validator('initiator_id', 'actor_id', 'target_id')
    @classmethod
    def _non_empty_ids(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError('must be a non-empty string')
        return v

    @field_validator('correlation_id', mode='before')
    @classmethod
    def _correlation_id_as_str(cls, v: object) -> str:
        if isinstance(v, UUID):
            return str(v)
        if isinstance(v, str):
            s = v.strip()
            if not s:
                raise ValueError('must be a non-empty string')
            return s
        raise ValueError('correlation_id must be a non-empty string')

    @model_validator(mode='after')
    def _causation_not_self_referential(self) -> Self:
        if self.causation_id is not None and self.causation_id == self.event_id:
            raise ValueError('causation_id must not equal event_id')
        return self


def new_root_log_event(
    *,
    event_type: str | None = None,
    level: LogLevel,
    message: str,
    component: str,
    initiator_type: LogParticipantKind,
    initiator_id: str,
    actor_type: LogParticipantKind,
    actor_id: str,
    target_type: LogParticipantKind,
    target_id: str,
    payload: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
    event_id: UUID | None = None,
    correlation_id: str | None = None,
) -> LogEvent:
    """Create a trace **root** event (new ``event_id`` and ``correlation_id``; ``causation_id`` is ``None``)."""
    eid = event_id if event_id is not None else uuid.uuid4()
    if correlation_id is not None:
        cid = correlation_id
    else:
        ctx_cid = current_correlation_id()
        cid = ctx_cid if ctx_cid is not None else str(uuid.uuid4())
    ts = timestamp if timestamp is not None else datetime.now(UTC)
    return LogEvent(
        event_id=eid,
        correlation_id=cid,
        causation_id=None,
        event_type=event_type,
        timestamp=ts,
        level=level,
        message=message,
        component=component,
        payload=dict(payload or {}),
        initiator_type=initiator_type,
        initiator_id=initiator_id,
        actor_type=actor_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
    )


def new_downstream_log_event_from_parent_id(
    *,
    parent_event_id: UUID,
    correlation_id: str,
    event_type: str | None = None,
    level: LogLevel,
    message: str,
    component: str,
    initiator_type: LogParticipantKind,
    initiator_id: str,
    actor_type: LogParticipantKind,
    actor_id: str,
    target_type: LogParticipantKind,
    target_id: str,
    payload: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
    event_id: UUID | None = None,
) -> LogEvent:
    """Downstream event when only the parent's ``event_id`` is known (e.g. connector RPC body)."""
    return LogEvent(
        event_id=event_id if event_id is not None else uuid.uuid4(),
        correlation_id=correlation_id,
        causation_id=parent_event_id,
        event_type=event_type,
        timestamp=timestamp if timestamp is not None else datetime.now(UTC),
        level=level,
        message=message,
        component=component,
        payload=dict(payload or {}),
        initiator_type=initiator_type,
        initiator_id=initiator_id,
        actor_type=actor_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
    )


def new_downstream_log_event(
    parent: LogEvent,
    *,
    event_type: str | None = None,
    level: LogLevel,
    message: str,
    component: str,
    initiator_type: LogParticipantKind,
    initiator_id: str,
    actor_type: LogParticipantKind,
    actor_id: str,
    target_type: LogParticipantKind,
    target_id: str,
    payload: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
    event_id: UUID | None = None,
) -> LogEvent:
    """Create a **downstream** event.

    New ``event_id``, same ``correlation_id`` as parent, ``causation_id`` = parent ``event_id``.
    """
    return LogEvent(
        event_id=event_id if event_id is not None else uuid.uuid4(),
        correlation_id=parent.correlation_id,
        causation_id=parent.event_id,
        event_type=event_type,
        timestamp=timestamp if timestamp is not None else datetime.now(UTC),
        level=level,
        message=message,
        component=component,
        payload=dict(payload or {}),
        initiator_type=initiator_type,
        initiator_id=initiator_id,
        actor_type=actor_type,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
    )

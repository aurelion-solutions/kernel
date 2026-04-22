# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API response schema for ``GET /api/v0/platform/events``."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from src.platform.events.schemas import EventEnvelope


class EventBufferEntryRead(BaseModel):
    """One event from the in-memory ring buffer as returned by the read endpoint."""

    model_config: ClassVar[ConfigDict] = ConfigDict(from_attributes=False, frozen=True)

    event_id: UUID
    event_type: str
    occurred_at: datetime
    correlation_id: str
    causation_id: UUID | None
    payload: dict[str, Any]
    initiator_kind: str | None
    initiator_id: str | None
    actor_kind: str | None
    actor_id: str | None
    target_kind: str | None
    target_id: str | None
    schema_version: str

    @classmethod
    def from_envelope(cls, env: EventEnvelope) -> EventBufferEntryRead:
        """Construct a read model from a frozen :class:`EventEnvelope`."""
        return cls(
            event_id=env.event_id,
            event_type=env.event_type,
            occurred_at=env.occurred_at,
            correlation_id=env.correlation_id,
            causation_id=env.causation_id,
            payload=dict(env.payload),
            initiator_kind=env.initiator_kind.value if env.initiator_kind is not None else None,
            initiator_id=env.initiator_id,
            actor_kind=env.actor_kind.value if env.actor_kind is not None else None,
            actor_id=env.actor_id,
            target_kind=env.target_kind.value if env.target_kind is not None else None,
            target_id=env.target_id,
            schema_version=env.schema_version,
        )

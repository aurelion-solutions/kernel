# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API response models for reading ``log_event_buffer`` rows."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class LogBufferEventRead(BaseModel):
    """One buffered log row as returned by GET ``/log-buffer``."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    event_id: UUID
    event_type: str
    timestamp: datetime
    level: str
    message: str
    component: str
    correlation_id: str
    causation_id: UUID | None
    payload: dict[str, Any]
    initiator_type: str
    initiator_id: str
    actor_type: str
    actor_id: str
    target_type: str
    target_id: str
    created_at: datetime

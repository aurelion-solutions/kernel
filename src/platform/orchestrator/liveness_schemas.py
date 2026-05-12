# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic payload schema for the ``executor.process.heartbeat`` domain event.

This schema describes only the *payload* dict nested inside
:class:`EventEnvelope`.payload for ``event_type='executor.process.heartbeat'``.
The envelope itself is constructed by :mod:`src.platform.orchestrator.liveness`.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator


class ExecutorHeartbeatPayload(BaseModel):
    """Payload for the ``executor.process.heartbeat`` event.

    Fields
    ------
    worker_id:
        ``<hostname>-<pid>-<slot_index>`` — identifies the executor process.
    slot_index:
        Zero-based concurrency slot within the process.
    started_at:
        UTC-aware timestamp of when the executor process started.
    pipelines_loaded:
        Number of pipeline definitions loaded at startup.
    """

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    worker_id: str
    slot_index: int
    started_at: datetime
    pipelines_loaded: int

    @field_validator('worker_id')
    @classmethod
    def _validate_worker_id(cls, v: str) -> str:
        if not v:
            raise ValueError('worker_id must be non-empty')
        return v

    @field_validator('slot_index')
    @classmethod
    def _validate_slot_index(cls, v: int) -> int:
        if v < 0:
            raise ValueError('slot_index must be >= 0')
        return v

    @field_validator('pipelines_loaded')
    @classmethod
    def _validate_pipelines_loaded(cls, v: int) -> int:
        if v < 0:
            raise ValueError('pipelines_loaded must be >= 0')
        return v

    @field_validator('started_at')
    @classmethod
    def _validate_started_at(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError('started_at must be timezone-aware (UTC)')
        return v

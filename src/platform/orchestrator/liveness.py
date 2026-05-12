# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Liveness heartbeat publisher for ``platform_executor_node``.

Observability carve-out rationale
----------------------------------
This module emits ``executor.process.heartbeat`` via :class:`EventService`
directly — it is not a ``service.py`` in a domain slice.  The same carve-out
applies as for ``connector.heartbeat``: the heartbeat is an operational /
observability event, not a domain state transition.  There is no transactional
context (no session, no DB writes), so the "only service.py emits events" rule
is relaxed here by explicit architectural decision (see TASK.md §3.1).

Three-segment event_type
-------------------------
``executor.process.heartbeat`` satisfies the ``^[a-z0-9_]+\\.[a-z0-9_]+\\.[a-z0-9_]+$``
regex enforced by :class:`EventEnvelope`.  The roadmap shorthand
``executor.heartbeat`` (two segments) would fail validation and is used in
prose only.

Usage
-----
Start a background task from the executor entrypoint::

    heartbeat_task = asyncio.create_task(
        heartbeat_publisher(
            events=event_service,
            logs=log_service,
            worker=worker,
            started_at=started_at,
            pipelines_loaded=len(pipelines),
            interval=interval,
            stop_event=heartbeat_stop_event,
        )
    )
    try:
        await work_loop(...)
    finally:
        heartbeat_stop_event.set()
        await heartbeat_task
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from src.platform.events.schemas import EventParticipantKind, new_event_envelope
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import merge_emit_component_trace_fields
from src.platform.orchestrator.liveness_schemas import ExecutorHeartbeatPayload

if TYPE_CHECKING:
    from src.platform.events.service import EventService
    from src.platform.logs.service import LogService
    from src.platform.orchestrator.runner import WorkerIdentity

EXECUTOR_HEARTBEAT_INTERVAL_DEFAULT_SECONDS: float = 60.0

_EVENT_TYPE = 'executor.process.heartbeat'
_COMPONENT = 'pipeline_orchestrator.liveness'


def _build_heartbeat_envelope(
    *,
    worker: WorkerIdentity,
    started_at: datetime,
    pipelines_loaded: int,
) -> object:
    """Build a heartbeat :class:`EventEnvelope` with a fresh correlation_id."""
    payload = ExecutorHeartbeatPayload(
        worker_id=worker.worker_id,
        slot_index=worker.slot_index,
        started_at=started_at,
        pipelines_loaded=pipelines_loaded,
    )
    return new_event_envelope(
        event_type=_EVENT_TYPE,
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=worker.worker_id,
        correlation_id=str(uuid4()),
        payload=payload.model_dump(mode='json'),
    )


async def heartbeat_publisher(
    *,
    events: EventService,
    logs: LogService,
    worker: WorkerIdentity,
    started_at: datetime,
    pipelines_loaded: int,
    interval: float,
    stop_event: asyncio.Event,
) -> None:
    """Emit ``executor.process.heartbeat`` periodically until ``stop_event`` is set.

    Emits once immediately on entry (positive liveness signal at startup), then
    every ``interval`` seconds until ``stop_event`` is set.

    Each failed emission is caught, logged as WARNING, and the loop continues.
    The publisher never crashes the executor process.
    """
    envelope = _build_heartbeat_envelope(
        worker=worker,
        started_at=started_at,
        pipelines_loaded=pipelines_loaded,
    )
    try:
        await events.emit(envelope)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 # allowed-broad: task-loop guard
        logs.emit_safe(  # allowed-emit-safe: observability
            LogLevel.WARNING,
            'executor heartbeat publish failed',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'worker_id': worker.worker_id, 'slot_index': worker.slot_index},
                component_id=_COMPONENT,
                target_id=worker.worker_id,
            ),
        )

    while not stop_event.is_set():
        done, _ = await asyncio.wait(
            [asyncio.ensure_future(stop_event.wait())],
            timeout=interval,
        )
        if stop_event.is_set():
            break

        envelope = _build_heartbeat_envelope(
            worker=worker,
            started_at=started_at,
            pipelines_loaded=pipelines_loaded,
        )
        try:
            await events.emit(envelope)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 # allowed-broad: task-loop guard
            logs.emit_safe(  # allowed-emit-safe: observability
                LogLevel.WARNING,
                'executor heartbeat publish failed',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'worker_id': worker.worker_id, 'slot_index': worker.slot_index},
                    component_id=_COMPONENT,
                    target_id=worker.worker_id,
                ),
            )

    logs.emit_safe(  # allowed-emit-safe: observability
        LogLevel.INFO,
        'executor heartbeat publisher stopped',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {'worker_id': worker.worker_id, 'slot_index': worker.slot_index},
            component_id=_COMPONENT,
            target_id=worker.worker_id,
        ),
    )

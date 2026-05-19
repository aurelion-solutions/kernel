# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Step-scoped LogService wrapper.

Injected into :class:`ActionContext.log_service` by the runner so that every
log line an action emits via ``ctx.log_service`` is automatically tagged
with ``target_type='system', target_id=<step_run_id>``. Without this
wrapper actions that omit participant fields are dropped by
:meth:`LogService.emit_log` (the underlying service requires all six
participant keys), and action lines that DO carry them generally point at
the run id — leaving the per-step Logs panel empty.

Explicit targeting wins: if the caller already populated
``target_type`` / ``target_id`` (and the matching initiator / actor pair)
in ``payload``, the wrapper leaves them alone. Otherwise it fills the
participant block with ``component_id`` (the engine name) as
initiator / actor and ``step_run_id`` as the target.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from src.platform.logs.schemas import LogEvent, LogLevel
from src.platform.logs.service import (
    PARTICIPANT_PAYLOAD_KEYS,
    LogService,
    merge_emit_component_trace_fields,
)


def _has_all_participants(payload: dict[str, Any] | None) -> bool:
    if payload is None:
        return False
    return all(payload.get(k) is not None for k in PARTICIPANT_PAYLOAD_KEYS)


class StepScopedLogService(LogService):
    """LogService façade that defaults ``target_id`` to a single step run.

    Constructed once per step dispatch. Forwards all emits to the
    underlying :class:`LogService`, injecting participant fields when the
    caller did not supply them.
    """

    def __init__(
        self,
        base: LogService,
        *,
        step_run_id: uuid.UUID,
        component_id: str,
    ) -> None:
        # Share the underlying sink so emits land on the same transport.
        super().__init__(sink=base.sink)
        self._step_run_id = str(step_run_id)
        self._component_id = component_id

    def emit_safe(
        self,
        level: LogLevel = LogLevel.INFO,
        message: str = '',
        component: str = '',
        payload: dict[str, Any] | None = None,
        *,
        timestamp: datetime | None = None,
        correlation_id: str | None = None,
        task_id: uuid.UUID | None = None,
        application_id: uuid.UUID | None = None,
        connector_type: str | None = None,
        result_id: uuid.UUID | None = None,
        request_id: str | None = None,
        exception_type: str | None = None,
        stacktrace: str | None = None,
    ) -> None:
        """Inject step-scoped participants if absent, then delegate."""
        super().emit_safe(
            level=level,
            message=message,
            component=component,
            payload=self._with_step_target(payload),
            timestamp=timestamp,
            correlation_id=correlation_id,
            task_id=task_id,
            application_id=application_id,
            connector_type=connector_type,
            result_id=result_id,
            request_id=request_id,
            exception_type=exception_type,
            stacktrace=stacktrace,
        )

    async def emit_log(
        self,
        level: LogLevel = LogLevel.INFO,
        message: str = '',
        component: str = '',
        payload: dict[str, Any] | None = None,
        *,
        timestamp: datetime | None = None,
        correlation_id: str | None = None,
        task_id: uuid.UUID | None = None,
        application_id: uuid.UUID | None = None,
        connector_type: str | None = None,
        result_id: uuid.UUID | None = None,
        request_id: str | None = None,
        exception_type: str | None = None,
        stacktrace: str | None = None,
    ) -> None:
        """Async variant — same injection rule as :meth:`emit_safe`."""
        await super().emit_log(
            level=level,
            message=message,
            component=component,
            payload=self._with_step_target(payload),
            timestamp=timestamp,
            correlation_id=correlation_id,
            task_id=task_id,
            application_id=application_id,
            connector_type=connector_type,
            result_id=result_id,
            request_id=request_id,
            exception_type=exception_type,
            stacktrace=stacktrace,
        )

    async def emit_event(self, event: LogEvent) -> None:
        """Pass-through — fully built events are not rewritten."""
        await super().emit_event(event)

    def emit_event_safe(self, event: LogEvent) -> None:
        """Pass-through — fully built events are not rewritten."""
        super().emit_event_safe(event)

    def _with_step_target(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        # Always stamp step_run_id into the payload as a side-channel field.
        # This is what the per-step UI filter actually queries via
        # ``payload->>'step_run_id'``, so emits that explicitly target
        # something else (a plan, an account, …) still get attributed to
        # the step that produced them.
        merged: dict[str, Any] = dict(payload or {})
        merged.setdefault('step_run_id', self._step_run_id)
        if _has_all_participants(merged):
            return merged
        return merge_emit_component_trace_fields(
            merged,
            component_id=self._component_id,
            target_id=self._step_run_id,
        )

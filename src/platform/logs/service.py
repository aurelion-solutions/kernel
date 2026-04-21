# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Platform log service for emitting structured log events."""

import asyncio
from datetime import UTC, datetime
import os
from typing import Any
from uuid import UUID

from src.platform.logs.factory import LogSinkFactory
from src.platform.logs.schemas import LogEvent, LogLevel, LogParticipantKind, new_root_log_event

# Keys LogService may take from ``payload`` and forward to :func:`new_root_log_event` only
# when all are present (mechanical pass-through; no defaults or interpretation).
_PARTICIPANT_PAYLOAD_KEYS: tuple[str, ...] = (
    'initiator_type',
    'initiator_id',
    'actor_type',
    'actor_id',
    'target_type',
    'target_id',
)


def _pop_participants_if_complete(merged: dict[str, Any]) -> dict[str, Any] | None:
    if not all(k in merged for k in _PARTICIPANT_PAYLOAD_KEYS):
        return None
    if any(merged[k] is None for k in _PARTICIPANT_PAYLOAD_KEYS):
        return None
    return {k: merged.pop(k) for k in _PARTICIPANT_PAYLOAD_KEYS}


def merge_emit_capability_trace_fields(
    payload: dict[str, Any],
    *,
    capability_id: str,
    target_id: str,
    target_type: str | None = None,
) -> dict[str, Any]:
    """Participant payload for capability-scoped operations (initiator = actor = capability)."""
    cap = LogParticipantKind.CAPABILITY.value
    tgt_type = target_type if target_type is not None else LogParticipantKind.SYSTEM.value
    return {
        **payload,
        'initiator_type': cap,
        'initiator_id': capability_id,
        'actor_type': cap,
        'actor_id': capability_id,
        'target_type': tgt_type,
        'target_id': target_id,
    }


def merge_emit_log_participant_fields(
    payload: dict[str, Any],
    *,
    actor_component: str,
    target_id: str = 'resource',
) -> dict[str, Any]:
    """Build a ``payload`` dict for :meth:`LogService.emit_log` / ``emit_safe``.

    LogService only forwards participant keys that are already present; it does not add
    them. Callers merge with this helper (or supply equivalent keys themselves).
    """
    return {
        **payload,
        'initiator_type': 'user',
        'initiator_id': 'platform',
        'actor_type': 'system',
        'actor_id': actor_component,
        'target_type': 'system',
        'target_id': target_id,
    }


def _schedule_or_run(coro: Any) -> None:
    """Schedule ``coro`` on the running loop, or run it synchronously if none exists.

    Used by the safe/fire-and-forget log methods so they remain callable from both
    async HTTP handlers and blocking pika consumer threads.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        # No running loop — blocking consumer thread context.
        try:
            asyncio.run(coro)
        except Exception:
            pass


class NoOpLogService:
    """Log service that does nothing. Use when logging is disabled."""

    def emit_event_safe(self, event: LogEvent) -> None:
        return None

    def emit_safe(
        self,
        # NOTE: This is a mechanical kwarg-shape refactor (Step 23 Phase 10).
        # The call sites still emit on aurelion.logs — NOT migrated to aurelion.events bus.
        # `event_type` was removed from this signature; use new_root_log_event(event_type=...)
        # + emit_event_safe(...) for legacy slices that need the field in the LogEvent body.
        level: LogLevel = LogLevel.INFO,
        message: str = '',
        component: str = '',
        payload: dict[str, Any] | None = None,
        *,
        timestamp: datetime | None = None,
        correlation_id: str | None = None,
        task_id: UUID | None = None,
        application_id: UUID | None = None,
        connector_type: str | None = None,
        result_id: UUID | None = None,
        request_id: str | None = None,
        exception_type: str | None = None,
        stacktrace: str | None = None,
    ) -> None:
        return None


noop_log_service = NoOpLogService()


def _get_provider() -> str:
    """Resolve provider name from env. Default is MQ."""
    return os.environ.get('AURELION_LOG_PROVIDER', 'mq')


class LogService:
    """Resolves sink via factory and emits :class:`LogEvent`.

    All public methods are **synchronous** at the call-site — they schedule the
    async work on the running event loop (fire-and-forget) or run it in a fresh
    loop if called from a blocking pika consumer thread.  This keeps the
    interface unchanged for consumer runtimes while using the async MQ publisher
    under the hood.

    Use :meth:`emit_event` (async) when the caller is already awaiting and wants
    delivery confirmation before continuing.
    """

    def __init__(
        self,
        factory: LogSinkFactory,
        provider_name: str | None = None,
    ) -> None:
        self._factory = factory
        self._provider_name = provider_name

    async def emit_event(self, event: LogEvent) -> None:
        """Emit a fully built event to the configured sink.  Re-raises on failure."""
        provider = self._provider_name or _get_provider()
        sink = self._factory.get(provider)
        await sink.emit(event)

    def emit_event_safe(self, event: LogEvent) -> None:
        """Emit fire-and-forget from any context (sync or async).

        Swallows all exceptions.
        """

        async def _safe() -> None:
            try:
                await self.emit_event(event)
            except Exception:
                pass

        _schedule_or_run(_safe())

    def emit_safe(
        self,
        # NOTE: `event_type` parameter removed (Step 23 Phase 10 — kwarg-shape refactor).
        # This is NOT a migration to aurelion.events bus; aurelion.logs semantics unchanged.
        level: LogLevel = LogLevel.INFO,
        message: str = '',
        component: str = '',
        payload: dict[str, Any] | None = None,
        *,
        timestamp: datetime | None = None,
        correlation_id: str | None = None,
        task_id: UUID | None = None,
        application_id: UUID | None = None,
        connector_type: str | None = None,
        result_id: UUID | None = None,
        request_id: str | None = None,
        exception_type: str | None = None,
        stacktrace: str | None = None,
    ) -> None:
        """Emit via configured sink, fire-and-forget.

        Callable from any context (sync consumer thread or async HTTP handler).
        Swallows all exceptions.
        """

        async def _safe() -> None:
            try:
                await self.emit_log(
                    level=level,
                    message=message,
                    component=component,
                    payload=payload,
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
            except Exception:
                pass

        _schedule_or_run(_safe())

    async def emit_log(
        self,
        # NOTE: `event_type` parameter removed (Step 23 Phase 10 — kwarg-shape refactor).
        # This is NOT a migration to aurelion.events bus; aurelion.logs semantics unchanged.
        # Legacy slices that need event_type in the LogEvent body call new_root_log_event(event_type=...)
        # + emit_event_safe(...) directly.
        level: LogLevel = LogLevel.INFO,
        message: str = '',
        component: str = '',
        payload: dict[str, Any] | None = None,
        *,
        timestamp: datetime | None = None,
        correlation_id: str | None = None,
        task_id: UUID | None = None,
        application_id: UUID | None = None,
        connector_type: str | None = None,
        result_id: UUID | None = None,
        request_id: str | None = None,
        exception_type: str | None = None,
        stacktrace: str | None = None,
    ) -> None:
        """Emit via configured sink.

        When ``payload`` includes all of
        ``initiator_type``, ``initiator_id``, ``actor_type``, ``actor_id``,
        ``target_type``, ``target_id`` (each non-``None``), those entries are
        removed from the stored payload and passed to :func:`new_root_log_event`.
        Otherwise this method returns without emitting (no defaults are applied).
        """
        merged: dict[str, Any] = dict(payload or {})
        participants = _pop_participants_if_complete(merged)
        if participants is None:
            return

        if task_id is not None:
            merged['task_id'] = str(task_id)
        if application_id is not None:
            merged['application_id'] = str(application_id)
        if connector_type is not None:
            merged['connector_type'] = connector_type
        if result_id is not None:
            merged['result_id'] = str(result_id)
        if request_id is not None:
            merged['request_id'] = request_id
        if exception_type is not None:
            merged['exception_type'] = exception_type
        if stacktrace is not None:
            merged['stacktrace'] = stacktrace

        ts = timestamp if timestamp is not None else datetime.now(UTC)
        event = new_root_log_event(
            level=level,
            message=message,
            component=component,
            payload=merged,
            timestamp=ts,
            correlation_id=correlation_id,
            **participants,
        )
        await self.emit_event(event)

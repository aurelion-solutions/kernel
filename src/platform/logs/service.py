# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Platform log service — entry point for app-log emission on the ``aurelion.logs`` bus.

Four-way split of emissions across the platform (documented here for code-level
readability; canonical definition lives in ``aurelion-mas/ARCH_CONTEXT.md``):

1. **App logs** — operational noise (DEBUG / INFO / WARNING / ERROR). Emitted via
   ``LogService.emit_log`` / ``LogService.emit_safe`` to the ``aurelion.logs``
   exchange. Best-effort retention. THIS MODULE owns this category.
2. **Domain events** — immutable business facts (``<layer>.<entity>.<verb>``).
   Emitted via ``EventService.emit`` to ``aurelion.events``. Compliance retention.
   Owned by ``src.platform.events.service``.
3. **Audit records** — regulatory entries under the ``audit.*`` routing-key
   namespace. Same bus as domain events (``aurelion.events``), same service
   (``EventService.emit``). No dedicated third bus.
4. **Trace metadata** — ``event_id`` / ``correlation_id`` / ``causation_id``.
   Not a bus; field-level values carried inside ``LogEvent`` and
   ``EventEnvelope``.

A single ``service.py`` call site picks exactly one of the three bus-visible
categories for any given action.

Internal layering (MQ path, service call → wire):

    emit_safe            — sync wrapper, fire-and-forget (caller affordance)
    emit_log             — envelope builder + sink resolution + await emit
    RabbitMQLogSink.emit — transport adapter
    AsyncRabbitMQPublisher.publish — wire
"""

import asyncio
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import LogEvent, LogLevel, LogParticipantKind, new_root_log_event

# Main event loop — set once during app startup so that fire-and-forget log
# calls from blocking consumer threads can schedule onto the correct loop
# instead of spawning a fresh one (which breaks aio_pika connections).
_main_loop: asyncio.AbstractEventLoop | None = None


def set_main_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop  # noqa: PLW0603
    _main_loop = loop


# Keys LogService may take from ``payload`` and forward to :func:`new_root_log_event` only
# when all are present (mechanical pass-through; no defaults or interpretation).
PARTICIPANT_PAYLOAD_KEYS: tuple[str, ...] = (
    'initiator_type',
    'initiator_id',
    'actor_type',
    'actor_id',
    'target_type',
    'target_id',
)
# Backwards-compatible alias (kept as a leading-underscore private name so the
# rest of the module's references don't need to change in this patch).
_PARTICIPANT_PAYLOAD_KEYS = PARTICIPANT_PAYLOAD_KEYS


def _pop_participants_if_complete(merged: dict[str, Any]) -> dict[str, Any] | None:
    if not all(k in merged for k in _PARTICIPANT_PAYLOAD_KEYS):
        return None
    if any(merged[k] is None for k in _PARTICIPANT_PAYLOAD_KEYS):
        return None
    return {k: merged.pop(k) for k in _PARTICIPANT_PAYLOAD_KEYS}


def merge_emit_component_trace_fields(
    payload: dict[str, Any],
    *,
    component_id: str,
    target_id: str,
    target_type: str | None = None,
) -> dict[str, Any]:
    """Participant payload for component-scoped operations (initiator = actor = component)."""
    cap = LogParticipantKind.CAPABILITY.value
    tgt_type = target_type if target_type is not None else LogParticipantKind.SYSTEM.value
    return {
        **payload,
        'initiator_type': cap,
        'initiator_id': component_id,
        'actor_type': cap,
        'actor_id': component_id,
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
    """Schedule ``coro`` on the running loop, or cross-thread-schedule if none exists.

    Used by the safe/fire-and-forget log methods so they remain callable from both
    async HTTP handlers and blocking pika consumer threads.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        # No running loop — we're in a blocking consumer thread.
        # Re-use the main app loop so that aio_pika connections (which are bound
        # to that loop) are not called from a foreign loop.
        if _main_loop is not None and _main_loop.is_running():
            asyncio.run_coroutine_threadsafe(coro, _main_loop)
        else:
            # Fallback: no running loop, no app loop registered (e.g. sync tests,
            # one-off scripts before lifespan). Run synchronously via asyncio.run
            # so the log actually reaches the sink.
            try:
                asyncio.run(coro)
            except Exception:  # noqa: BLE001 # allowed-broad: event handler swallow
                pass


def _run_fire_and_forget(coro: Coroutine[Any, Any, None]) -> None:
    """Schedule ``coro`` with all exceptions swallowed.

    Single definition of fire-and-forget semantics for LogService.emit_safe and
    LogService.emit_event_safe. Wraps the coroutine in try/except that swallows
    any Exception, then hands off to _schedule_or_run for loop-or-run dispatch.
    """

    async def _runner() -> None:
        try:
            await coro
        except Exception:  # noqa: BLE001 # allowed-broad: event handler swallow
            pass

    _schedule_or_run(_runner())


class NoOpLogService:
    """Log service that does nothing. Use when logging is disabled."""

    def emit_event_safe(self, event: LogEvent) -> None:
        return None

    def emit_safe(
        self,
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


class LogService:
    """Emits :class:`LogEvent` to the configured :class:`LogSink`.

    All public methods are **synchronous** at the call-site — they schedule the
    async work on the running event loop (fire-and-forget) or run it in a fresh
    loop if called from a blocking pika consumer thread.

    Use :meth:`emit_event` (async) when the caller is already awaiting and wants
    delivery confirmation before continuing.
    """

    def __init__(self, sink: LogSink) -> None:
        self._sink = sink

    @property
    def sink(self) -> LogSink:
        """The configured underlying sink.

        Exposed so a wrapper (e.g. ``StepScopedLogService``) can construct a
        subclass that delegates to the same transport without reaching into
        a private attribute.
        """
        return self._sink

    async def emit_event(self, event: LogEvent) -> None:
        """Emit a fully built event to the configured sink. Re-raises on failure."""
        await self._sink.emit(event)

    def emit_event_safe(self, event: LogEvent) -> None:
        """Emit fire-and-forget from any context (sync or async).

        Swallows all exceptions.
        """
        _run_fire_and_forget(self.emit_event(event))

    def emit_safe(
        self,
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
        _run_fire_and_forget(
            self.emit_log(
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
        await self._sink.emit(event)

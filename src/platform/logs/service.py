# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Platform log service for emitting structured log events."""

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


class NoOpLogService:
    """Log service that does nothing. Use when logging is disabled."""

    def emit_event_safe(self, event: LogEvent) -> None:
        return None

    def emit_safe(
        self,
        event_type: str,
        level: LogLevel,
        message: str,
        component: str,
        payload: dict[str, Any],
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
    """Resolves sink via factory and emits :class:`LogEvent`."""

    def __init__(
        self,
        factory: LogSinkFactory,
        provider_name: str | None = None,
    ) -> None:
        self._factory = factory
        self._provider_name = provider_name

    def emit_event(self, event: LogEvent) -> None:
        """Emit a fully built event to the configured sink."""
        provider = self._provider_name or _get_provider()
        sink = self._factory.get(provider)
        sink.emit(event)

    def emit_event_safe(self, event: LogEvent) -> None:
        """Like :meth:`emit_event` but swallows failures. Never raises."""
        try:
            self.emit_event(event)
        except Exception:
            pass

    def emit_log(
        self,
        event_type: str,
        level: LogLevel,
        message: str,
        component: str,
        payload: dict[str, Any],
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
        merged: dict[str, Any] = dict(payload)
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
            event_type=event_type,
            level=level,
            message=message,
            component=component,
            payload=merged,
            timestamp=ts,
            correlation_id=correlation_id,
            **participants,
        )
        self.emit_event(event)

    def emit_safe(
        self,
        event_type: str,
        level: LogLevel,
        message: str,
        component: str,
        payload: dict[str, Any],
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
        """Like :meth:`emit_log` but swallows failures. Never raises."""
        try:
            self.emit_log(
                event_type=event_type,
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

    def log_info(
        self,
        event_type: str,
        message: str,
        component: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Convenience: emit at INFO level.

        ``**kwargs`` are forwarded to :meth:`emit_log` (optional metadata only).
        """
        self.emit_log(event_type, LogLevel.INFO, message, component, payload, **kwargs)

    def log_warning(
        self,
        event_type: str,
        message: str,
        component: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Convenience: emit at WARNING level.

        ``**kwargs`` are forwarded to :meth:`emit_log` (optional metadata only).
        """
        self.emit_log(event_type, LogLevel.WARNING, message, component, payload, **kwargs)

    def log_error(
        self,
        event_type: str,
        message: str,
        component: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Convenience: emit at ERROR level.

        ``**kwargs`` are forwarded to :meth:`emit_log` (optional metadata only).
        """
        self.emit_log(event_type, LogLevel.ERROR, message, component, payload, **kwargs)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Decode-and-apply handler for the EAS incremental projection consumer.

Subscribes to ``aurelion.events``. Decodes each message body as
:class:`~src.platform.events.schemas.EventEnvelope`, filters by routing key
against a small relevant-set, maps to ``apply_incremental_change`` kwargs,
and drives one apply call per message.

Invariant clarification — "only service.py emits events":
The three runtime-emitted log records (``eas.projection.consumer.parse_error``,
``.missing_fact_id``, ``.missing_initiative_id``, ``.apply_failed``) are
**operational/observability logs** on ``aurelion.logs``, not domain events.
They describe the consumer process's own lifecycle, not a change in EAS state.
``apply_failed`` in particular is emitted from the handler **after**
``session.rollback()`` — deliberately outside the business transaction
boundary, because a rollback means no domain event was emitted in the first
place. Domain events (``eas.projection.completed`` /
``eas.projection.failed``) continue to be emitted strictly inside
``service.apply_incremental_change`` pre-commit — on ``aurelion.events``.

Dual-service DI is the ratified architecture (C20): ``log_service`` for
observability on ``aurelion.logs``, ``event_service`` for domain events
on ``aurelion.events``. Two buses, two services.

Event emission discipline (per ARCH_CONTEXT):
- ``parse_error``, ``missing_fact_id``, ``missing_initiative_id`` are emitted
  outside any DB session (no session exists yet when parsing fails).
- ``apply_failed`` is emitted after ``session.rollback()`` — outside the
  business transaction that was rolled back.
- ``eas.projection.completed`` / ``eas.projection.failed`` are emitted by
  the service inside the session, post-flush, pre-commit
  (ARCH_CONTEXT §Event emission placement).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import threading
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.engines.access_effective.schemas import IncrementalApplyKind
from src.engines.access_effective.service import EffectiveAccessProjectionService
from src.platform.events.schemas import EventEnvelope
from src.platform.events.service import EventService
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService

_COMPONENT = 'eas.projection.consumer'

_EVENT_TYPES_UPSERT: frozenset[str] = frozenset(
    {
        'inventory.access_fact.created',
        'inventory.initiative.created',
        'inventory.initiative.updated',
    }
)
_EVENT_TYPES_INVALIDATE_FACT: frozenset[str] = frozenset({'inventory.access_fact.revoked'})
_EVENT_TYPES_INVALIDATE_INITIATIVE: frozenset[str] = frozenset({'inventory.initiative.expired'})
_EVENT_TYPES_RELEVANT: frozenset[str] = (
    _EVENT_TYPES_UPSERT | _EVENT_TYPES_INVALIDATE_FACT | _EVENT_TYPES_INVALIDATE_INITIATIVE
)

# ---------------------------------------------------------------------------
# Background-loop helpers (mirror buffer_consumer.py pattern)
# ---------------------------------------------------------------------------
# pika runs sync callbacks; we must not call ``asyncio.run`` per message:
# each run creates and destroys an event loop while SQLAlchemy's asyncpg
# pool keeps connections bound to the previous loop, which then raises
# "Event loop is closed" during pool teardown.
# See src/platform/logs/buffer_consumer.py L19-21 for the canonical note.
_BG_LOOP_LOCK = threading.Lock()
_bg_loop: asyncio.AbstractEventLoop | None = None


def _worker_loop() -> asyncio.AbstractEventLoop:
    """Return (creating on first call) a long-lived background asyncio loop.

    Pattern lifted from src/platform/logs/buffer_consumer.py — asyncio.run
    per pika message breaks the asyncpg pool (see that module's header
    comment). Each runtime process owns one such loop for its process
    lifetime.
    """
    global _bg_loop
    with _BG_LOOP_LOCK:
        if _bg_loop is not None and _bg_loop.is_running():
            return _bg_loop
        _bg_loop = None

        ready = threading.Event()
        holder: list[asyncio.AbstractEventLoop] = []

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            holder.append(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(
            target=_run,
            name='aurelion-eas-projection-bg',
            daemon=True,
        )
        thread.start()
        ready.wait()
        _bg_loop = holder[0]
        return _bg_loop


# ---------------------------------------------------------------------------
# Public entry point (sync façade for pika callback)
# ---------------------------------------------------------------------------


def handle_message(
    body: bytes,
    *,
    routing_key: str,
    session_factory: async_sessionmaker[AsyncSession],
    projection_service_factory: Callable[[AsyncSession, EventService], EffectiveAccessProjectionService],
    log_service: LogService,
    event_service: EventService,
) -> None:
    """Decode one MQ message body and drive one apply call (sync façade).

    Wraps ``_handle_message_async`` via ``asyncio.run_coroutine_threadsafe``
    on the module-level background loop. Ack-and-log: the caller in
    ``main.py`` acks unconditionally after this returns.
    """
    loop = _worker_loop()
    fut = asyncio.run_coroutine_threadsafe(
        _handle_message_async(
            body,
            routing_key=routing_key,
            session_factory=session_factory,
            projection_service_factory=projection_service_factory,
            log_service=log_service,
            event_service=event_service,
        ),
        loop,
    )
    fut.result(timeout=120)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _parse_uuid_field(
    envelope: EventEnvelope,
    field_name: str,
    log_event_name: str,
    log_service: LogService,
) -> UUID | None:
    """Extract and parse a UUID string field from the envelope payload.

    Emits a WARNING via ``log_service`` if the field is missing, non-string,
    or not a valid UUID, then returns ``None``. Returns the parsed UUID on
    success.
    """
    raw = envelope.payload.get(field_name)
    if not isinstance(raw, str):
        log_service.emit_safe(
            level=LogLevel.WARNING,
            message=f'Missing or non-string {field_name} in {envelope.event_type!r} payload',
            component=_COMPONENT,
            payload={'event_type': envelope.event_type, 'event_id': str(envelope.event_id)},
        )
        return None
    try:
        return UUID(raw)
    except ValueError:
        log_service.emit_safe(
            level=LogLevel.WARNING,
            message=f'Invalid UUID for {field_name} in {envelope.event_type!r} payload',
            component=_COMPONENT,
            payload={'event_type': envelope.event_type, 'raw_value': raw},
        )
        return None


# ---------------------------------------------------------------------------
# Private async handler — the seam Phase 10 rewrites
# ---------------------------------------------------------------------------


async def _handle_message_async(
    body: bytes,
    *,
    routing_key: str,
    session_factory: async_sessionmaker[AsyncSession],
    projection_service_factory: Callable[[AsyncSession, EventService], EffectiveAccessProjectionService],
    log_service: LogService,
    event_service: EventService,
) -> None:
    """Decode EventEnvelope → map to kwargs → open session → call apply → commit."""
    # Step 1+2 — decode JSON and validate as EventEnvelope in one pass.
    # model_validate_json handles both JSON syntax errors and schema validation
    # errors; it also correctly coerces string UUIDs / datetimes that model_validate
    # with strict=True would reject.
    try:
        envelope = EventEnvelope.model_validate_json(body)
    except (ValidationError, UnicodeDecodeError, ValueError):
        log_service.emit_safe(
            level=LogLevel.ERROR,
            message='Failed to validate MQ message body as EventEnvelope',
            component=_COMPONENT,
            payload={'raw_preview': str(body[:200]), 'routing_key': routing_key},
        )
        return

    # Step 3 — routing-key mismatch guard
    if routing_key != envelope.event_type:
        log_service.emit_safe(
            level=LogLevel.WARNING,
            message=f'routing_key {routing_key!r} != envelope.event_type {envelope.event_type!r}; skipping',
            component=_COMPONENT,
            payload={'routing_key': routing_key, 'envelope_event_type': envelope.event_type},
        )
        return

    # Step 4 — noise filter by routing key (silent)
    if routing_key not in _EVENT_TYPES_RELEVANT:
        return

    # Step 5 — dispatch based on routing key
    is_initiative_event = routing_key in _EVENT_TYPES_INVALIDATE_INITIATIVE
    routed_initiative_id: UUID | None = None
    routed_access_fact_id: UUID | None = None
    routed_kind: IncrementalApplyKind

    if is_initiative_event:
        routed_initiative_id = _parse_uuid_field(
            envelope, 'initiative_id', 'eas.projection.consumer.missing_initiative_id', log_service
        )
        if routed_initiative_id is None:
            return
        routed_kind = IncrementalApplyKind.INVALIDATE_INITIATIVE
        scope_key = str(routed_initiative_id)
    else:
        # inventory.access_fact.revoked uses 'fact_id'; older events used 'access_fact_id'.
        # Try 'fact_id' first (Step 13+), fall back to 'access_fact_id' for backwards compat.
        fact_id_field = 'fact_id' if 'fact_id' in envelope.payload else 'access_fact_id'
        routed_access_fact_id = _parse_uuid_field(
            envelope, fact_id_field, 'eas.projection.consumer.missing_fact_id', log_service
        )
        if routed_access_fact_id is None:
            return
        routed_kind = (
            IncrementalApplyKind.INVALIDATE_FACT
            if routing_key in _EVENT_TYPES_INVALIDATE_FACT
            else IncrementalApplyKind.UPSERT
        )
        scope_key = str(routed_access_fact_id)

    # Step 6 — parse correlation_id (best-effort; fall back to None)
    correlation_uuid: UUID | None = None
    try:
        correlation_uuid = UUID(envelope.correlation_id)
    except (ValueError, AttributeError):
        pass

    # Step 7 — open session, call apply, commit
    try:
        async with session_factory() as session:
            try:
                service = projection_service_factory(session, event_service)
                await service.apply_incremental_change(
                    change_kind=routed_kind,
                    observed_at=envelope.occurred_at,
                    access_fact_id=routed_access_fact_id,
                    initiative_id=routed_initiative_id,
                    correlation_id=correlation_uuid,
                    causation_event_id=envelope.event_id,
                )
                await session.commit()
            except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
                await session.rollback()
                log_service.emit_safe(
                    level=LogLevel.ERROR,
                    message=f'EAS apply failed for {routing_key!r}: {type(exc).__name__}',
                    component=_COMPONENT,
                    payload={
                        'scope_key': scope_key,
                        'event_type': routing_key,
                        'exception_type': type(exc).__name__,
                    },
                )
    except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
        # Session factory itself failed (e.g. DB unreachable)
        log_service.emit_safe(
            level=LogLevel.ERROR,
            message=f'EAS session factory failed for {routing_key!r}: {type(exc).__name__}',
            component=_COMPONENT,
            payload={
                'scope_key': scope_key,
                'event_type': routing_key,
                'exception_type': type(exc).__name__,
            },
        )

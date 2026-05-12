# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pipeline matcher — async MQ consumer for waiter resolution and MQ-triggered pipelines.

Design invariants
-----------------
- Single-replica gate via session-level pg_advisory_lock (``_MATCHER_LOCK_KEY``).
  A second replica acquires the same lock key and enters a warm-standby sleep
  loop, emitting ``pipeline.matcher.lock_contention`` each cycle.
- All state is in Postgres + RabbitMQ: crash-safe and restart-safe.
- Effect (a) — waiter resolution — and effect (b) — MQ trigger firing — run in
  **independent** transactions so a failure in one does not roll back the other.
- Per-waiter and per-trigger SAVEPOINT isolation within each effect batch.
- ``emit_safe`` for all logging; no ``print``, no bare ``logging.getLogger`` calls.
- Poison messages are ack'd (no DLQ this step); logged with payload-length counter.
- ``matcher.py`` MUST NOT call ``get_settings()`` or
  ``register_default_providers()`` at import time. All connection params come in
  via function arguments supplied by the lifespan composition root.

Advisory lock key: "AURELMAT" encoded as a 64-bit integer.
ASCII: 41=A 55=U 52=R 45=E 4C=L 4D=M 41=A 54=T
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import json
from typing import TYPE_CHECKING, Any
import uuid

import aio_pika
import aio_pika.abc
import sqlalchemy as sa
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_component_trace_fields
from src.platform.orchestrator.models import PipelineTriggerSource

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.platform.events.schemas import EventEnvelope
    from src.platform.orchestrator.loader import PipelineDefinition
    from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# ASCII hex: 41=A 55=U 52=R 45=E 4C=L 4D=M 41=A 54=T → "AURELMAT"
_MATCHER_LOCK_KEY = 0x4155_5245_4C4D_4154

_COMPONENT = 'pipeline_matcher'

_LOCK_RETRY_SLEEP_SECONDS = 1.0


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _payload_satisfies_match(match: Mapping[str, Any], payload: Mapping[str, Any]) -> bool:
    """Return True if every key/value in ``match`` is present and equal in ``payload``.

    Mirrors Postgres ``match <@ payload`` (containment) semantics for flat
    and nested dicts.  Primitive list comparison is set-based (order-independent).
    Nested list comparison is deferred — not supported in this phase.

    Empty match always returns True (matches any payload).
    """
    if not match:
        return True

    for key, match_val in match.items():
        if key not in payload:
            return False
        payload_val = payload[key]

        if isinstance(match_val, Mapping):
            if not isinstance(payload_val, Mapping):
                return False
            if not _payload_satisfies_match(match_val, payload_val):
                return False
        elif isinstance(match_val, list):
            # Primitive list set-containment: every element of match_val must
            # appear in payload_val.  Nested list comparison is deferred.
            if not isinstance(payload_val, list):
                return False
            for item in match_val:
                if item not in payload_val:
                    return False
        else:
            if payload_val != match_val:
                return False

    return True


def _extract_args_from_payload(
    args_from_payload: Mapping[str, str],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Extract args from payload using dotted-path keys.

    Each value in ``args_from_payload`` is a dotted path like ``foo.bar``
    resolved against ``payload``.  Missing paths yield ``None`` (not an error).
    """
    result: dict[str, Any] = {}
    for arg_name, dotted_path in args_from_payload.items():
        parts = dotted_path.split('.')
        val: Any = payload
        for part in parts:
            if isinstance(val, Mapping) and part in val:
                val = val[part]
            else:
                val = None
                break
        result[arg_name] = val
    return result


def find_matching_mq_triggers(
    loader_defs: Mapping[str, PipelineDefinition],
    routing_key: str,
    payload: Mapping[str, Any],
) -> list[tuple[PipelineDefinition, Mapping[str, Any]]]:
    """Return (definition, trigger) pairs for mq triggers matching routing_key + payload.

    Pure function — no I/O.  Called by ``matcher_tick`` to decide which
    pipelines to start.
    """
    matches: list[tuple[PipelineDefinition, Mapping[str, Any]]] = []
    for defn in loader_defs.values():
        for trigger in defn.triggers:
            if trigger.get('type') != 'mq':
                continue
            if trigger.get('routing_key') != routing_key:
                continue
            match_spec: Mapping[str, Any] = trigger.get('match', {})
            if _payload_satisfies_match(match_spec, payload):
                matches.append((defn, trigger))
    return matches


# ---------------------------------------------------------------------------
# Effect batch helpers
# ---------------------------------------------------------------------------


async def _resolve_waiter_batch(
    session_factory: async_sessionmaker[AsyncSession],
    service_factory: Callable[[AsyncSession], PipelineOrchestratorService],
    log_service: LogService | NoOpLogService,
    step_ids: list[uuid.UUID],
    payload: dict[str, Any],
    correlation_id: str | None,
) -> None:
    """Resolve each waiter in its own SAVEPOINT; failures logged and skipped."""
    if not step_ids:
        return

    async with session_factory() as session:
        service = service_factory(session)
        for step_run_id in step_ids:
            try:
                async with session.begin_nested():
                    resolved = await service.resolve_pipeline_event_waiter(
                        step_run_id,
                        payload,
                        correlation_id=correlation_id,
                    )
                if resolved:
                    log_service.emit_safe(
                        level=LogLevel.INFO,
                        message='Matcher resolved waiter',
                        component=_COMPONENT,
                        payload=merge_emit_component_trace_fields(
                            {
                                'event_name': 'pipeline.matcher.waiter_resolved',
                                'step_run_id': str(step_run_id),
                            },
                            component_id=_COMPONENT,
                            target_id=str(step_run_id),
                        ),
                        correlation_id=correlation_id,
                    )
                else:
                    log_service.emit_safe(
                        level=LogLevel.INFO,
                        message='Matcher skipped waiter (race lost)',
                        component=_COMPONENT,
                        payload=merge_emit_component_trace_fields(
                            {
                                'event_name': 'pipeline.matcher.waiter_resolve_skipped',
                                'step_run_id': str(step_run_id),
                            },
                            component_id=_COMPONENT,
                            target_id=str(step_run_id),
                        ),
                        correlation_id=correlation_id,
                    )
            except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
                log_service.emit_safe(
                    level=LogLevel.WARNING,
                    message='Matcher failed to resolve waiter',
                    component=_COMPONENT,
                    payload=merge_emit_component_trace_fields(
                        {
                            'event_name': 'pipeline.matcher.waiter_resolve_failed',
                            'step_run_id': str(step_run_id),
                            'error': str(exc),
                        },
                        component_id=_COMPONENT,
                        target_id=str(step_run_id),
                    ),
                    correlation_id=correlation_id,
                )
        await session.commit()


async def _fire_mq_trigger_batch(
    session_factory: async_sessionmaker[AsyncSession],
    service_factory: Callable[[AsyncSession], PipelineOrchestratorService],
    log_service: LogService | NoOpLogService,
    triggers: list[tuple[PipelineDefinition, Mapping[str, Any]]],
    payload: dict[str, Any],
    correlation_id: str | None,
) -> None:
    """Fire each MQ trigger in its own session; duplicate deliveries are skipped."""
    if not triggers:
        return

    for defn, trigger in triggers:
        try:
            args: dict[str, Any] = dict(trigger.get('args', {}))
            args_from_payload = trigger.get('args_from_payload', {})
            if args_from_payload:
                args.update(_extract_args_from_payload(args_from_payload, payload))

            async with session_factory() as session:
                service = service_factory(session)
                result = await service.create_pipeline_run(
                    defn.name,
                    defn.version,
                    args,
                    trigger_source=PipelineTriggerSource.mq,
                    correlation_id=correlation_id,
                )
                await session.commit()

            if result.created:
                log_service.emit_safe(
                    level=LogLevel.INFO,
                    message='Matcher fired MQ trigger',
                    component=_COMPONENT,
                    payload=merge_emit_component_trace_fields(
                        {
                            'event_name': 'pipeline.matcher.mq_trigger_fired',
                            'pipeline_name': defn.name,
                            'run_id': str(result.run.id),
                        },
                        component_id=_COMPONENT,
                        target_id=defn.name,
                    ),
                    correlation_id=correlation_id,
                )
            else:
                log_service.emit_safe(
                    level=LogLevel.DEBUG,
                    message='Matcher MQ trigger duplicate delivery — skipped',
                    component=_COMPONENT,
                    payload=merge_emit_component_trace_fields(
                        {
                            'event_name': 'pipeline.matcher.mq_trigger_duplicate',
                            'pipeline_name': defn.name,
                            'run_id': str(result.run.id),
                        },
                        component_id=_COMPONENT,
                        target_id=defn.name,
                    ),
                    correlation_id=correlation_id,
                )
        except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
            log_service.emit_safe(
                level=LogLevel.ERROR,
                message='Matcher MQ trigger firing failed',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {
                        'pipeline_name': defn.name,
                        'error': str(exc),
                    },
                    component_id=_COMPONENT,
                    target_id=defn.name,
                ),
                correlation_id=correlation_id,
            )


# ---------------------------------------------------------------------------
# matcher_tick
# ---------------------------------------------------------------------------


async def matcher_tick(
    *,
    event_type: str,
    routing_key: str,
    payload: dict[str, Any],
    correlation_id: str | None,
    causation_id: str | None,
    session_factory: async_sessionmaker[AsyncSession],
    defs_provider: Callable[[], Mapping[str, PipelineDefinition]],
    service_factory: Callable[[AsyncSession], PipelineOrchestratorService],
    log_service: LogService | NoOpLogService,
) -> None:
    """Orchestrate effect (a) then effect (b) for one delivered message.

    Effect (a) and (b) run in independent transactions; (b) failure cannot
    roll back (a).
    """
    defs = defs_provider()

    # --- (a) Waiter resolution ---
    async with session_factory() as session:
        service = service_factory(session)
        step_ids = await service.find_matching_waiter_step_ids(event_type, payload)
        # Session used only for read here — no commit needed.

    await _resolve_waiter_batch(
        session_factory,
        service_factory,
        log_service,
        step_ids,
        payload,
        correlation_id,
    )

    # --- (b) MQ-trigger pipeline start ---
    matched_triggers = find_matching_mq_triggers(defs, routing_key, payload)
    await _fire_mq_trigger_batch(
        session_factory,
        service_factory,
        log_service,
        matched_triggers,
        payload,
        correlation_id,
    )


# ---------------------------------------------------------------------------
# matcher_loop
# ---------------------------------------------------------------------------


async def matcher_loop(
    *,
    mq_url: str,
    events_exchange: str,
    matcher_queue: str,
    binding_keys: list[str],
    session_factory: async_sessionmaker[AsyncSession],
    defs_provider: Callable[[], Mapping[str, PipelineDefinition]],
    service_factory: Callable[[AsyncSession], PipelineOrchestratorService],
    log_service: LogService | NoOpLogService,
) -> None:
    """Async MQ consumer loop.

    Acquires ``pg_advisory_lock(_MATCHER_LOCK_KEY)`` on a dedicated long-lived
    session.  A second replica that cannot acquire the lock becomes a warm
    standby and sleeps 1 s between attempts.

    Per delivery:
    1. Decode JSON body → EventEnvelope.model_validate (best-effort).
    2. Derive event_type (envelope field, fall back to delivery routing key).
    3. Call matcher_tick (independent transactions per effect).
    4. ack the message (even on failure — no DLQ this step).

    Exits cleanly on asyncio.CancelledError.
    """

    from src.platform.events.schemas import EventEnvelope  # noqa: PLC0415

    log_service.emit_safe(
        level=LogLevel.INFO,
        message='Matcher loop starting',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {'event_name': 'pipeline.matcher.started'},
            component_id=_COMPONENT,
            target_id='matcher',
        ),
    )

    connection: aio_pika.abc.AbstractRobustConnection | None = None
    lock_session: AsyncSession | None = None

    try:
        # Connect to RabbitMQ.
        connection = await aio_pika.connect_robust(mq_url)
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=1)

        # Declare exchange + queue + bindings.
        exchange = await channel.declare_exchange(
            events_exchange,
            type=aio_pika.ExchangeType.TOPIC,
            durable=True,
        )
        queue = await channel.declare_queue(matcher_queue, durable=True)
        for key in binding_keys:
            await queue.bind(exchange, routing_key=key)

        # Open a dedicated persistent session for the advisory lock.
        _lock_cm = session_factory()
        lock_session = await _lock_cm.__aenter__()

        while True:
            lock_row = await lock_session.execute(sa.select(sa.func.pg_try_advisory_lock(_MATCHER_LOCK_KEY)))
            lock_acquired: bool = bool(lock_row.scalar())

            if lock_acquired:
                break

            log_service.emit_safe(
                level=LogLevel.DEBUG,
                message='Matcher lock contention — warm standby',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {
                        'event_name': 'pipeline.matcher.lock_contention',
                        'lock_acquired': False,
                    },
                    component_id=_COMPONENT,
                    target_id='matcher',
                ),
            )
            await asyncio.sleep(_LOCK_RETRY_SLEEP_SECONDS)

        log_service.emit_safe(
            level=LogLevel.INFO,
            message='Matcher lock acquired — consuming',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'lock_acquired': True},
                component_id=_COMPONENT,
                target_id='matcher',
            ),
        )

        # Main consume loop.
        async with queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process(ignore_processed=True):
                    routing_key_delivered: str = message.routing_key or ''

                    # Decode + validate envelope (best-effort).
                    envelope: EventEnvelope | None = None
                    try:
                        raw = json.loads(message.body)
                        envelope = EventEnvelope.model_validate(raw)
                    except Exception:  # noqa: BLE001 # allowed-broad: provider boundary
                        pass

                    if envelope is not None:
                        event_type = envelope.event_type
                        payload: dict[str, Any] = dict(envelope.payload) if envelope.payload else {}
                        correlation_id: str | None = envelope.correlation_id
                        causation_id: str | None = str(envelope.event_id)
                    else:
                        # Malformed envelope — fall back to routing key.
                        event_type = routing_key_delivered
                        payload = {}
                        correlation_id = None
                        causation_id = None

                    payload_len = len(message.body)
                    log_service.emit_safe(
                        level=LogLevel.DEBUG,
                        message='Matcher received event',
                        component=_COMPONENT,
                        payload=merge_emit_component_trace_fields(
                            {
                                'event_name': 'pipeline.matcher.event_received',
                                'event_type': event_type,
                                'routing_key': routing_key_delivered,
                                'payload_bytes': payload_len,
                            },
                            component_id=_COMPONENT,
                            target_id='matcher',
                        ),
                        correlation_id=correlation_id,
                    )

                    try:
                        await matcher_tick(
                            event_type=event_type,
                            routing_key=routing_key_delivered,
                            payload=payload,
                            correlation_id=correlation_id,
                            causation_id=causation_id,
                            session_factory=session_factory,
                            defs_provider=defs_provider,
                            service_factory=service_factory,
                            log_service=log_service,
                        )
                    except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
                        log_service.emit_safe(
                            level=LogLevel.ERROR,
                            message='Matcher tick failed — acking poison message',
                            component=_COMPONENT,
                            payload=merge_emit_component_trace_fields(
                                {
                                    'event_name': 'pipeline.matcher.tick_failed',
                                    'event_type': event_type,
                                    'error': str(exc),
                                    'payload_bytes': payload_len,
                                },
                                component_id=_COMPONENT,
                                target_id='matcher',
                            ),
                            correlation_id=correlation_id,
                        )

    except asyncio.CancelledError:
        raise
    finally:
        # Release advisory lock on the dedicated session.
        if lock_session is not None:
            try:
                await lock_session.execute(sa.select(sa.func.pg_advisory_unlock(_MATCHER_LOCK_KEY)))
                await lock_session.close()
            except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
                pass

        if connection is not None:
            try:
                await connection.close()
            except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
                pass

        log_service.emit_safe(
            level=LogLevel.INFO,
            message='Matcher loop stopped',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'event_name': 'pipeline.matcher.stopped'},
                component_id=_COMPONENT,
                target_id='matcher',
            ),
        )

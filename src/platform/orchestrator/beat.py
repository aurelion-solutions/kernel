# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pipeline beat — periodic schedule-firing tick.

Design invariants
-----------------
- Beat NEVER executes steps, NEVER writes to orchestrator tables directly, and
  NEVER logs outside LogService.emit_safe.
- All state is in Postgres: dedupe is DB-backed (one schedule run per cron
  window) and survives restart.
- Multi-replica safety via pg_try_advisory_lock: only one replica fires per
  tick window.
- ``now`` is always injected into public functions (never datetime.now() inside)
  so tests can control time without monkey-patching.
- Broad-except in beat_loop and per-pipeline guards is intentional and
  annotated per ARCH_CONTEXT broad-except discipline.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
import uuid

from croniter import CroniterBadCronError, CroniterBadDateError, croniter
import sqlalchemy as sa
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_component_trace_fields
from src.platform.orchestrator._durations import parse_duration
from src.platform.orchestrator.models import PipelineTriggerSource

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.platform.orchestrator.loader import PipelineDefinition
    from src.platform.orchestrator.service import PipelineOrchestratorService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BEAT_TICK_INTERVAL_SECONDS = 10.0  # hardcoded; follow-up to RuntimeSettings

# Advisory lock key: "AURELBEA7" encoded as a 64-bit integer.
# ASCII hex mapping: 41=A, 55=U, 52=R, 45=E, 4C=L, 42=B, 45=E, 41=A, 37=7
_BEAT_LOCK_KEY = 0x4155_5245_4C42_4541  # noqa: E501 — 0x41='A', 0x55='U', 0x52='R', 0x45='E', 0x4C='L', 0x42='B', 0x45='E', 0x41='A' + trailing 7 → AURELBEA7

_COMPONENT = 'pipeline_beat'

# Fixed epoch for every-N anchoring: deterministic across restarts and replicas.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class BeatScheduleParseError(Exception):
    """Raised when a cron expression or ``every`` duration cannot be parsed."""


# ---------------------------------------------------------------------------
# BeatTickResult
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BeatTickResult:
    """Summary of one beat_tick execution."""

    fired_run_ids: list[uuid.UUID] = field(default_factory=list)
    skipped_count: int = 0
    lock_acquired: bool = True
    lock_contention_count: int = 0  # observability: how many pipelines saw a contention (0 when lock not acquired)
    expired_run_ids: list[uuid.UUID] = field(default_factory=list)
    expire_failure_count: int = 0


# ---------------------------------------------------------------------------
# compute_previous_fire_point
# ---------------------------------------------------------------------------


def compute_previous_fire_point(
    now: datetime,
    *,
    cron: str | None = None,
    every: str | None = None,
) -> datetime:
    """Compute the most recent fire-point that should have fired before ``now``.

    Exactly one of ``cron`` or ``every`` must be provided.

    For ``every``:
        Uses an epoch-anchored floor so the result is deterministic across
        restarts and replicas.  Epoch = 1970-01-01T00:00:00Z.

    For ``cron``:
        Uses ``croniter`` to find the most recent past occurrence.

    Returns a timezone-aware UTC datetime.

    Raises BeatScheduleParseError on malformed input.
    """
    if (cron is None) == (every is None):
        raise BeatScheduleParseError('exactly one of cron or every must be provided')

    if every is not None:
        try:
            window = parse_duration(every)
        except ValueError as exc:
            raise BeatScheduleParseError(f'invalid every duration: {every!r}') from exc

        window_secs = window.total_seconds()
        # Ensure now is UTC-aware.
        now_utc = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)
        elapsed = (now_utc - _EPOCH).total_seconds()
        floored = (elapsed // window_secs) * window_secs
        return _EPOCH + timedelta(seconds=floored)

    # cron path
    assert cron is not None
    try:
        # croniter.get_prev(datetime) returns the previous occurrence.
        # We use start_time = now so get_prev returns the last time before now.
        it = croniter(cron, now)
        fire_point: datetime = it.get_prev(datetime)
    except (CroniterBadCronError, CroniterBadDateError, ValueError) as exc:
        raise BeatScheduleParseError(f'invalid cron expression: {cron!r}') from exc

    # Ensure UTC-aware.
    if fire_point.tzinfo is None:
        fire_point = fire_point.replace(tzinfo=UTC)
    else:
        fire_point = fire_point.astimezone(UTC)
    return fire_point


# ---------------------------------------------------------------------------
# already_fired_in_window
# ---------------------------------------------------------------------------


async def already_fired_in_window(
    session: AsyncSession,
    pipeline_name: str,
    pipeline_version: int,
    fire_point: datetime,
) -> bool:
    """Return True if a schedule-triggered run already exists in the current window.

    Filtering on trigger_source='schedule' is load-bearing: manual/MQ runs in
    the same window MUST NOT block the schedule.
    """
    from src.platform.orchestrator.models import PipelineRun  # local import: avoids circular at module load

    stmt = sa.select(sa.literal(1)).where(
        PipelineRun.pipeline_name == pipeline_name,
        PipelineRun.pipeline_version == pipeline_version,
        PipelineRun.trigger_source == PipelineTriggerSource.schedule,
        PipelineRun.created_at >= fire_point,
    )
    result = await session.execute(stmt)
    return result.scalar() is not None


# ---------------------------------------------------------------------------
# fire_schedule
# ---------------------------------------------------------------------------


async def fire_schedule(
    service: PipelineOrchestratorService,
    defn: PipelineDefinition,
    schedule_trigger: Mapping[str, Any],
    *,
    now: datetime,
    correlation_id: str | None = None,
) -> uuid.UUID | None:
    """Fire one schedule trigger and return the new run_id, or None on dedupe.

    Injects ``_scheduled_at`` (ISO-format UTC) into args so downstream steps
    can template ``${args._scheduled_at}``.  The leading underscore is reserved
    for beat-injected system fields and cannot collide with user-declared args
    (schema regex ``^[a-z][a-z0-9_]*$`` forbids leading underscore).

    Returns ``run.id`` when ``created=True``, else ``None`` (race dedupe).
    """
    cid = correlation_id if correlation_id is not None else uuid.uuid4().hex
    now_utc = now.astimezone(UTC) if now.tzinfo is not None else now.replace(tzinfo=UTC)

    args: dict[str, Any] = {**schedule_trigger.get('args', {}), '_scheduled_at': now_utc.isoformat()}

    result = await service.create_pipeline_run(
        defn.name,
        defn.version,
        args,
        trigger_source=PipelineTriggerSource.schedule,
        correlation_id=cid,
    )
    if result.created:
        return result.run.id
    return None


# ---------------------------------------------------------------------------
# sweep_timeouts
# ---------------------------------------------------------------------------


async def sweep_timeouts(
    session: AsyncSession,
    service_factory: Callable[[AsyncSession], PipelineOrchestratorService],
    log_service: LogService | NoOpLogService,
    *,
    now: datetime,
) -> tuple[list[uuid.UUID], int]:
    """Expire all overdue pipeline_event_waiters in a single bounded batch.

    Each waiter is processed inside its own SAVEPOINT so a poisoned row does
    not roll back work already done in this batch.  The outer transaction is
    committed (or rolled back) by the caller (beat_tick).

    Returns ``(expired_run_ids, failure_count)``.
    """
    service = service_factory(session)
    step_ids = await service.list_expired_waiter_step_ids(now)

    expired_run_ids: list[uuid.UUID] = []
    failure_count = 0

    for step_run_id in step_ids:
        try:
            async with session.begin_nested():
                ok, run_id = await service.expire_event_waiter(step_run_id)
            if ok and run_id is not None:
                expired_run_ids.append(run_id)
                log_service.emit_safe(  # allowed-emit-safe: observability
                    level=LogLevel.INFO,
                    message='Beat sweep expired waiter',
                    component=_COMPONENT,
                    payload=merge_emit_component_trace_fields(
                        {'step_run_id': str(step_run_id), 'run_id': str(run_id)},
                        component_id=_COMPONENT,
                        target_id=str(run_id),
                    ),
                )
        except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
            failure_count += 1
            log_service.emit_safe(  # allowed-emit-safe: best-effort warning
                level=LogLevel.ERROR,
                message='Beat sweep failed to expire waiter',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'step_run_id': str(step_run_id), 'error': str(exc)},
                    component_id=_COMPONENT,
                    target_id='beat',
                ),
            )

    return expired_run_ids, failure_count


# ---------------------------------------------------------------------------
# beat_tick
# ---------------------------------------------------------------------------


async def beat_tick(
    session_factory: async_sessionmaker[AsyncSession],
    defs: Mapping[str, PipelineDefinition],
    service_factory: Callable[[AsyncSession], PipelineOrchestratorService],
    log_service: LogService | NoOpLogService,
    *,
    now: datetime | None = None,
) -> BeatTickResult:
    """One beat tick: acquire advisory lock, fire due schedules, release lock.

    Uses a single session for the entire tick so advisory lock and pipeline-run
    inserts share the same transaction.  The caller (beat_loop) owns no
    additional session.

    Beat owns the commit here: service.create_pipeline_run flushes; beat_tick
    commits so the rows persist (ARCH_CONTEXT 'Services flush, callers commit').
    """
    if now is None:
        now = datetime.now(UTC)

    result = BeatTickResult()

    async with session_factory() as session:
        # Acquire per-tick advisory lock — non-blocking (try).
        lock_row = await session.execute(sa.select(sa.func.pg_try_advisory_lock(_BEAT_LOCK_KEY)))
        lock_acquired: bool = bool(lock_row.scalar())
        result.lock_acquired = lock_acquired

        if not lock_acquired:
            log_service.emit_safe(  # allowed-emit-safe: observability
                level=LogLevel.DEBUG,
                message='Beat tick — lock contention, sibling won',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {},
                    component_id=_COMPONENT,
                    target_id='beat',
                ),
            )
            return result

        try:
            for defn in defs.values():
                schedule_triggers = [t for t in defn.triggers if t.get('type') == 'schedule']
                for trigger in schedule_triggers:
                    try:
                        cron: str | None = trigger.get('cron')
                        every: str | None = trigger.get('every')
                        fire_point = compute_previous_fire_point(now, cron=cron, every=every)

                        fired = await already_fired_in_window(session, defn.name, defn.version, fire_point)
                        if fired:
                            result.skipped_count += 1
                            log_service.emit_safe(  # allowed-emit-safe: observability
                                level=LogLevel.DEBUG,
                                message='Schedule already fired in window',
                                component=_COMPONENT,
                                payload=merge_emit_component_trace_fields(
                                    {
                                        'pipeline_name': defn.name,
                                        'fire_point': fire_point.isoformat(),
                                    },
                                    component_id=_COMPONENT,
                                    target_id=defn.name,
                                ),
                            )
                            continue

                        service = service_factory(session)
                        run_id = await fire_schedule(
                            service,
                            defn,
                            trigger,
                            now=now,
                        )
                        if run_id is not None:
                            result.fired_run_ids.append(run_id)
                            log_service.emit_safe(  # allowed-emit-safe: observability
                                level=LogLevel.INFO,
                                message='Schedule fired',
                                component=_COMPONENT,
                                payload=merge_emit_component_trace_fields(
                                    {
                                        'pipeline_name': defn.name,
                                        'run_id': str(run_id),
                                        'fire_point': fire_point.isoformat(),
                                    },
                                    component_id=_COMPONENT,
                                    target_id=defn.name,
                                ),
                            )
                        else:
                            # Race: another path (concurrent tick) won.
                            result.skipped_count += 1

                    except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
                        log_service.emit_safe(  # allowed-emit-safe: best-effort warning
                            level=LogLevel.ERROR,
                            message='Schedule firing failed for pipeline',
                            component=_COMPONENT,
                            payload=merge_emit_component_trace_fields(
                                {'pipeline_name': defn.name, 'error': str(exc)},
                                component_id=_COMPONENT,
                                target_id=defn.name,
                            ),
                        )

            # Timeout sweep — runs inside the same lock + session, before commit.
            expired_ids, failed = await sweep_timeouts(session, service_factory, log_service, now=now)
            result.expired_run_ids = expired_ids
            result.expire_failure_count = failed

            await session.commit()
        finally:
            await session.execute(sa.select(sa.func.pg_advisory_unlock(_BEAT_LOCK_KEY)))

    return result


# ---------------------------------------------------------------------------
# beat_loop
# ---------------------------------------------------------------------------


async def beat_loop(
    session_factory: async_sessionmaker[AsyncSession],
    defs_provider: Callable[[], Mapping[str, PipelineDefinition]],
    service_factory: Callable[[AsyncSession], PipelineOrchestratorService],
    log_service: LogService | NoOpLogService,
    *,
    interval: float = _BEAT_TICK_INTERVAL_SECONDS,
) -> None:
    """Periodic beat loop — fires due schedules every ``interval`` seconds.

    ``defs_provider`` is called each tick so a future hot-reloader can swap
    definitions without touching beat.

    Exits cleanly on asyncio.CancelledError (lifespan shutdown).
    """
    while True:
        await asyncio.sleep(interval)
        try:
            defs = defs_provider()
            await beat_tick(session_factory, defs, service_factory, log_service)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
            log_service.emit_safe(  # allowed-emit-safe: best-effort warning
                level=LogLevel.ERROR,
                message='Beat tick crashed',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'error': str(exc)},
                    component_id=_COMPONENT,
                    target_id='beat',
                ),
            )

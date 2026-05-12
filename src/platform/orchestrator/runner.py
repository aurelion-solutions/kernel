# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pipeline runner — work loop and dispatch for the native orchestrator.

Design invariants
-----------------
- This module is a library: it MUST NOT call load_dotenv / register_default_providers
  / get_settings at import time.  All dependencies are injected by the caller.
- Three-session protocol per step: session A claims the run (committed before the run
  body starts); session B persists the StepRun row + resolves templates and is
  committed before action dispatch (so the row is visible to later sessions); the
  action runs inside session C — on success C commits the success transition, on
  failure C is rolled back and a fresh session D persists ``mark_step_failed``.
- Only ``LogService.emit_safe`` is used for observability — no print, no logging.
- Broad-except in ``work_loop`` and the action dispatch boundary is intentional
  and annotated per coding rules.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
from dataclasses import dataclass
from datetime import UTC, datetime
import os
import re
import socket
from typing import TYPE_CHECKING, Any
import uuid

from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_component_trace_fields
from src.platform.orchestrator._durations import parse_duration
from src.platform.orchestrator.models import PipelineRun, PipelineRunStatus
from src.platform.orchestrator.registry import ACTION_REGISTRY, ActionContext
from src.platform.orchestrator.service import PipelineOrchestratorService, _run_cancelled_event

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from src.platform.events.service import EventService
    from src.platform.orchestrator.loader import PipelineDefinition

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_COMPONENT = 'pipeline_orchestrator.runner'

# Interval at which the runner refreshes pipeline_runs.last_heartbeat_at while
# a step action is in-flight.  Hard-coded per phase_18.md:481 — not an
# operator knob.  When this becomes tunable (post-Phase 18), move it to
# RuntimeSettingsConfig + FIELD_META + Alembic ensure_defaults().
_HEARTBEAT_REFRESH_INTERVAL_SECONDS = 3.0

# Batch size for the stale-run reclaim sweep per tick.
_RECLAIM_SWEEP_BATCH_LIMIT = 50

# Matches ${args.X} and ${steps.<sname>.result.<path>}
# Re-declared from loader.py to keep runner.py dependency-free of the loader
# (runner calls into loader via injected protocol, not direct import).
_TEMPLATE_RE = re.compile(r'\$\{(args\.[a-zA-Z0-9_]+|steps\.[a-z][a-z0-9_]*\.result\.[a-zA-Z0-9_.]+)\}')

# _parse_duration is the canonical parser; re-exported here so callers that
# imported it from runner keep working without change.
_parse_duration = parse_duration


# ---------------------------------------------------------------------------
# WorkerIdentity
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkerIdentity:
    """Immutable identity for one worker slot.

    Format: ``<hostname>-<pid>-<slot_index>`` — the same string stored in
    ``pipeline_runs.worker_id``.
    """

    worker_id: str
    hostname: str
    pid: int
    slot_index: int

    @classmethod
    def create(cls, slot_index: int = 0) -> WorkerIdentity:
        """Build a WorkerIdentity from the current process environment."""
        hostname = socket.gethostname()
        pid = os.getpid()
        worker_id = f'{hostname}-{pid}-{slot_index}'
        return cls(worker_id=worker_id, hostname=hostname, pid=pid, slot_index=slot_index)


# ---------------------------------------------------------------------------
# Pipeline loader protocol
# ---------------------------------------------------------------------------


class PipelineLoaderProtocol:
    """Structural typing shim — the runner calls ``.get(name, version)`` only."""

    def get(self, name: str, version: int) -> PipelineDefinition | None:  # pragma: no cover
        raise NotImplementedError


# ---------------------------------------------------------------------------
# RunHandle — tracks the in-flight run across work_loop / drain_active_run
# ---------------------------------------------------------------------------


@dataclass
class RunHandle:
    """Mutable handle written by run_one_iteration, read by drain_active_run.

    ``run_id`` is None when no run is currently in-flight.
    ``completion`` is set() by run_one_iteration's finally block so that
    drain_active_run can wait on it.
    """

    run_id: uuid.UUID | None = None
    completion: asyncio.Event = dataclasses.field(default_factory=asyncio.Event)


# ---------------------------------------------------------------------------
# Template resolver
# ---------------------------------------------------------------------------


def _resolve_templates(
    value: Any,
    *,
    pipeline_args: dict[str, Any],
    step_results: dict[str, dict[str, Any]],
) -> Any:
    """Recursively resolve ``${...}`` template expressions in *value*.

    Supported references (validated at load time by the loader):
    - ``${args.X}``                    → ``pipeline_args[X]``
    - ``${steps.<s>.result.<p>.<q>}``  → ``step_results[s]['result'][p][q]``

    Type preservation: when the entire string is a single ``${...}`` reference
    the resolved native value is returned as-is (preserving ints, lists, etc.).
    Mixed-text strings stringify the resolved value via ``str()``.

    Missing keys propagate as ``KeyError`` to the caller (runner marks step
    failed).  The loader's static validation guarantees reachability, so this
    should only trigger on runtime mismatches (schema drift, bad action output).
    """
    if isinstance(value, dict):
        return {
            k: _resolve_templates(v, pipeline_args=pipeline_args, step_results=step_results) for k, v in value.items()
        }
    if isinstance(value, list):
        return [_resolve_templates(item, pipeline_args=pipeline_args, step_results=step_results) for item in value]
    if not isinstance(value, str):
        return value

    # Pure single-reference: return native type without stringification.
    pure_match = _TEMPLATE_RE.fullmatch(value)
    if pure_match:
        return _resolve_ref(pure_match.group(1), pipeline_args, step_results)

    # Mixed string: replace each match with its stringified value.
    def _replacer(m: re.Match[str]) -> str:
        return str(_resolve_ref(m.group(1), pipeline_args, step_results))

    return _TEMPLATE_RE.sub(_replacer, value)


def _resolve_ref(
    ref: str,
    pipeline_args: dict[str, Any],
    step_results: dict[str, dict[str, Any]],
) -> Any:
    """Resolve a single template reference expression."""
    if ref.startswith('args.'):
        key = ref[len('args.') :]
        return pipeline_args[key]

    # steps.<sname>.result.<p1>.<p2>...
    parts = ref.split('.')
    # parts: ['steps', sname, 'result', p1, p2, ...]
    step_name = parts[1]
    path = parts[3:]  # everything after 'result'
    node: Any = step_results[step_name]['result']
    for segment in path:
        node = node[segment]
    return node


# ---------------------------------------------------------------------------
# claim_one_pending_run
# ---------------------------------------------------------------------------


async def claim_one_pending_run(
    session: AsyncSession,
    *,
    worker_id: str,
    events: EventService,
    logs: LogService | NoOpLogService,
    correlation_id: str | None,
) -> PipelineRun | None:
    """Thin wrapper that builds a PipelineOrchestratorService and claims a run."""
    svc = PipelineOrchestratorService(session=session, events=events, logs=logs)
    return await svc.claim_pending_run(worker_id, correlation_id=correlation_id)


# ---------------------------------------------------------------------------
# reclaim_sweep_tick
# ---------------------------------------------------------------------------


async def reclaim_sweep_tick(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    events: EventService,
    logs: LogService | NoOpLogService,
    batch_limit: int = _RECLAIM_SWEEP_BATCH_LIMIT,
    correlation_id: str | None = None,
) -> None:
    """Scan for stale pipeline runs and atomically release ownership.

    Uses one session to peek (list_stale_run_ids) and a fresh session per row
    to reclaim (reclaim_stale_run).  A failure on row N must not prevent
    rows N+1..N+k from being processed.

    This function is called at the top of every work_loop tick, before
    run_one_iteration, so a reclaimed run is available for the next claim.
    """
    # Session S0: cooperative peek — no FOR UPDATE.
    async with session_factory() as session_s0:
        svc_s0 = PipelineOrchestratorService(session=session_s0, events=events, logs=logs)
        stale_ids = await svc_s0.list_stale_run_ids(limit=batch_limit)
        await session_s0.commit()

    if stale_ids:
        logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.DEBUG,
            message='Reclaim sweep tick',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'found': len(stale_ids)},
                component_id=_COMPONENT,
                target_id='runner',
            ),
        )

    for rid in stale_ids:
        try:
            async with session_factory() as session_rid:
                svc_rid = PipelineOrchestratorService(session=session_rid, events=events, logs=logs)
                await svc_rid.reclaim_stale_run(rid, correlation_id=correlation_id)
                await session_rid.commit()
        except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
            logs.emit_safe(  # allowed-emit-safe: observability
                level=LogLevel.WARNING,
                message='Reclaim sweep row failed',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'run_id': str(rid), 'error': str(exc)},
                    component_id=_COMPONENT,
                    target_id=str(rid),
                ),
            )


# ---------------------------------------------------------------------------
# drain_active_run
# ---------------------------------------------------------------------------


async def drain_active_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    completion_event: asyncio.Event,
    events: EventService,
    logs: LogService | NoOpLogService,
    drain_timeout: float,
) -> None:
    """Wait for an in-flight run to finish; self-release on timeout.

    The heartbeat refresher must continue running until drain_active_run
    returns — the caller is responsible for NOT stopping the refresher until
    after this function returns.  This ensures the row stays legitimately stale
    (older than _RECLAIM_STALE_THRESHOLD_SECONDS) only AFTER the drain
    timeout triggers, which allows self-release via reclaim_stale_run.

    On a clean exit (completion_event is set before timeout) the run is already
    in a terminal state — no reclaim needed.

    On timeout the run row is still in ``running`` status (the heartbeat was
    stopped just before we call reclaim_stale_run) — reclaim_stale_run will
    release it once the heartbeat has aged out.

    Note: because drain_timeout default is 60s and _RECLAIM_STALE_THRESHOLD_SECONDS
    is 10s, the row will be stale long before self-release runs.  The dependency
    60 > 10 is intentional and documented here.
    """
    try:
        await asyncio.wait_for(asyncio.shield(completion_event.wait()), timeout=drain_timeout)
        logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.INFO,
            message='Drain completed cleanly',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id)},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
        )
    except TimeoutError:
        logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.WARNING,
            message='Drain timeout — self-releasing run',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id), 'drain_timeout': drain_timeout},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
        )
        try:
            async with session_factory() as session_drain:
                svc_drain = PipelineOrchestratorService(session=session_drain, events=events, logs=logs)
                await svc_drain.reclaim_stale_run(run_id)
                await session_drain.commit()
        except Exception as exc:  # noqa: BLE001 # allowed-broad: best-effort cleanup
            logs.emit_safe(  # allowed-emit-safe: observability
                level=LogLevel.WARNING,
                message='Self-release failed',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'run_id': str(run_id), 'error': str(exc)},
                    component_id=_COMPONENT,
                    target_id=str(run_id),
                ),
            )


# ---------------------------------------------------------------------------
# _heartbeat_refresher
# ---------------------------------------------------------------------------


async def _heartbeat_refresher(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    worker_id: str,
    events: EventService,
    logs: LogService | NoOpLogService,
    stop_event: asyncio.Event,
    cancel_event: asyncio.Event,
    interval_seconds: float = _HEARTBEAT_REFRESH_INTERVAL_SECONDS,
) -> None:
    """Periodically refresh ``pipeline_runs.last_heartbeat_at`` while a step runs.

    Detects dead processes, not hung actions; action-level timeouts are out of
    scope.

    After each heartbeat tick, reads the current run status.  If status is
    ``cancelling``, sets ``cancel_event`` and returns — the main loop will tear
    down the in-flight step task.

    Opens its own session per tick so the refresher is independent of the
    action's session lifetime.  Survives ``False`` returns and DB exceptions —
    both are treated as transient blips and only emit a WARNING log.  The loop
    exits only when *stop_event* is set or a cancelling status is detected.

    CPU-bound limitation: if a step holds the event loop without yielding,
    ``cancel_event.set()`` fires but ``step_task.cancel()`` may not preempt
    until the step yields.  This is identical to the existing
    asyncio.CancelledError risk and is documented in TASK.md.
    # TODO(Phase 19+): investigate executor offload for CPU-bound actions.
    """
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            return  # stop_event was set during the wait
        except TimeoutError:
            pass  # interval elapsed — tick the heartbeat

        try:
            async with session_factory() as session:
                svc = PipelineOrchestratorService(session=session, events=events, logs=logs)
                ok = await svc.refresh_heartbeat(run_id, worker_id)
                row_status = await svc.read_status(run_id)
                await session.commit()
            if not ok:
                logs.emit_safe(  # allowed-emit-safe: observability
                    level=LogLevel.WARNING,
                    message='Heartbeat refresh missed (rowcount=0)',
                    component=_COMPONENT,
                    payload=merge_emit_component_trace_fields(
                        {'run_id': str(run_id), 'worker_id': worker_id},
                        component_id=_COMPONENT,
                        target_id=str(run_id),
                    ),
                )
            if row_status == PipelineRunStatus.cancelling:
                logs.emit_safe(  # allowed-emit-safe: observability
                    level=LogLevel.INFO,
                    message='Cancel detected — aborting step',
                    component=_COMPONENT,
                    payload=merge_emit_component_trace_fields(
                        {'run_id': str(run_id), 'worker_id': worker_id},
                        component_id=_COMPONENT,
                        target_id=str(run_id),
                    ),
                )
                cancel_event.set()
                return  # refresher's job is done; main loop will tear down
        except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
            logs.emit_safe(  # allowed-emit-safe: observability
                level=LogLevel.WARNING,
                message='Heartbeat refresh raised',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'run_id': str(run_id), 'worker_id': worker_id, 'error': str(exc)},
                    component_id=_COMPONENT,
                    target_id=str(run_id),
                ),
            )


# ---------------------------------------------------------------------------
# run_one_iteration
# ---------------------------------------------------------------------------


async def run_one_iteration(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worker: WorkerIdentity,
    pipeline_loader: Any,
    events: EventService,
    logs: LogService | NoOpLogService,
    correlation_id: str | None = None,
    run_handle: RunHandle | None = None,
) -> str:
    """Execute one work-loop iteration.

    Returns:
        ``"idle"``             — no pending run found; caller should back off.
        ``"completed"``        — run executed and reached the completed state.
        ``"failed"``           — run executed but failed (action error, missing
                                 definition, unsupported step kind, invalid timeout).
        ``"awaiting_event"``   — run parked on a wait_for_event step; worker
                                 slot is released and the run waits for an
                                 external event (Steps 16/17).

    ``run_handle`` is an optional mutable handle shared with ``work_loop``.
    When provided, ``run_id`` is written into it after a successful claim and
    cleared in the ``finally`` block.  This allows ``drain_active_run`` to know
    which run is currently in-flight.
    """
    # --- Session A: claim --------------------------------------------------
    async with session_factory() as session_a:
        run = await claim_one_pending_run(
            session_a,
            worker_id=worker.worker_id,
            events=events,
            logs=logs,
            correlation_id=correlation_id,
        )
        await session_a.commit()

    if run is None:
        return 'idle'

    run_id = run.id
    # Signal to work_loop that a run is now in-flight.
    if run_handle is not None:
        run_handle.run_id = run_id
        run_handle.completion.clear()

    try:
        return await _execute_run(
            session_factory,
            worker=worker,
            pipeline_loader=pipeline_loader,
            events=events,
            logs=logs,
            correlation_id=correlation_id,
            run=run,
        )
    finally:
        # Signal drain_active_run that this iteration is done (success or failure).
        if run_handle is not None:
            run_handle.completion.set()
            run_handle.run_id = None


async def _execute_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    worker: WorkerIdentity,
    pipeline_loader: Any,
    events: EventService,
    logs: LogService | NoOpLogService,
    correlation_id: str | None,
    run: PipelineRun,
) -> str:
    """Body of run_one_iteration after claim — extracted so run_handle finally is clean."""
    run_id = run.id

    logs.emit_safe(  # allowed-emit-safe: observability
        level=LogLevel.DEBUG,
        message='Runner claimed run',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {'run_id': str(run_id), 'worker_id': worker.worker_id},
            component_id=_COMPONENT,
            target_id=str(run_id),
        ),
        correlation_id=correlation_id,
    )

    # --- Session B: resolve definition + execute steps -----------------------
    # Resolve pipeline definition (no write needed here).
    defn = pipeline_loader.get(run.pipeline_name, run.pipeline_version)
    if defn is None:
        async with session_factory() as session_fail:
            svc_fail = PipelineOrchestratorService(session=session_fail, events=events, logs=logs)
            await svc_fail.mark_pipeline_failed(
                run_id,
                error='pipeline definition not found',
                correlation_id=correlation_id,
            )
            await session_fail.commit()
        return 'failed'

    step_results: dict[str, dict[str, Any]] = {}

    for step in defn.steps:
        step_name: str = step['name']

        if step.get('type') == 'wait_for_event':
            return await _park_wait_for_event(
                session_factory,
                run_id=run_id,
                step=step,
                step_name=step_name,
                pipeline_args=dict(run.args or {}),
                step_results=step_results,
                events=events,
                logs=logs,
                correlation_id=correlation_id,
            )

        if 'engine' in step and 'action' in step:
            pass  # engine_call branch continues below
        else:
            step_kind_label = step.get('type') or step.get('kind') or '<unknown>'
            async with session_factory() as session_fail:
                svc_fail = PipelineOrchestratorService(session=session_fail, events=events, logs=logs)
                await svc_fail.mark_pipeline_failed(
                    run_id,
                    error=f'unsupported step kind: {step_kind_label}',
                    correlation_id=correlation_id,
                )
                await session_fail.commit()
            return 'failed'

        # Resolve templated args.
        raw_args: dict[str, Any] = dict(step.get('args', {}))
        try:
            resolved_args = _resolve_templates(
                raw_args,
                pipeline_args=dict(run.args or {}),
                step_results=step_results,
            )
        except (KeyError, TypeError) as exc:
            async with session_factory() as session_fail:
                svc_fail = PipelineOrchestratorService(session=session_fail, events=events, logs=logs)
                await svc_fail.mark_pipeline_failed(
                    run_id,
                    error=f'template resolution failed for step {step_name!r}: {exc}',
                    correlation_id=correlation_id,
                )
                await session_fail.commit()
            return 'failed'

        # Create step run record in a committed transaction so it survives
        # an action failure rollback (step_run must exist in DB when
        # mark_step_failed is called in session_c).
        async with session_factory() as session_step:
            svc_step = PipelineOrchestratorService(session=session_step, events=events, logs=logs)
            step_run = await svc_step.create_step_run(
                run_id,
                step_name,
                resolved_args,
                correlation_id=correlation_id,
            )
            await session_step.commit()

        step_run_id = step_run.id
        step_run_attempt = step_run.attempt

        logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.DEBUG,
            message='Runner step dispatched',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {
                    'run_id': str(run_id),
                    'step_run_id': str(step_run_id),
                    'engine': step['engine'],
                    'action': step['action'],
                },
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )

        # Session B: run the action.  May be rolled back on exception.
        # The refresher runs in parallel and keeps last_heartbeat_at alive.
        # cancel_event is set by the refresher when it detects 'cancelling'.
        _hb_stop = asyncio.Event()
        _cancel_event = asyncio.Event()
        _hb_task = asyncio.create_task(
            _heartbeat_refresher(
                session_factory,
                run_id=run_id,
                worker_id=worker.worker_id,
                events=events,
                logs=logs,
                stop_event=_hb_stop,
                cancel_event=_cancel_event,
            )
        )
        try:
            async with session_factory() as session_b:
                ctx = ActionContext(
                    session=session_b,
                    log_service=logs,  # type: ignore[arg-type]
                    pipeline_run_id=run_id,
                    step_run_id=step_run_id,
                    attempt=step_run_attempt,
                    worker_id=worker.worker_id,
                )

                step_task = asyncio.create_task(
                    ACTION_REGISTRY.dispatch(
                        step['engine'],
                        step['action'],
                        resolved_args,
                        ctx,
                    )
                )
                cancel_wait_task = asyncio.create_task(_cancel_event.wait())

                done, pending = await asyncio.wait(
                    {step_task, cancel_wait_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Cancel wins — abort the in-flight step.
                if cancel_wait_task in done and step_task not in done:
                    step_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):  # noqa: BLE001 # allowed-broad: best-effort cleanup
                        await step_task

                    # Cancel the pending cancel_wait_task if somehow still there.
                    cancel_wait_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await cancel_wait_task

                    # Roll back session_b (action's in-flight state).
                    await session_b.rollback()

                    logs.emit_safe(  # allowed-emit-safe: observability
                        level=LogLevel.INFO,
                        message='Runner step cancelled',
                        component=_COMPONENT,
                        payload=merge_emit_component_trace_fields(
                            {'run_id': str(run_id), 'step_run_id': str(step_run_id)},
                            component_id=_COMPONENT,
                            target_id=str(run_id),
                        ),
                        correlation_id=correlation_id,
                    )

                    # Persist cancel — fresh session so action rollback is complete.
                    async with session_factory() as session_cancel:
                        svc_cancel = PipelineOrchestratorService(session=session_cancel, events=events, logs=logs)
                        await svc_cancel.mark_step_cancelled(step_run_id, correlation_id=correlation_id)
                        await svc_cancel.mark_pipeline_cancelled(run_id, correlation_id=correlation_id)
                        await events.emit(_run_cancelled_event(run_id, 'cancelling', correlation_id))
                        await session_cancel.commit()

                    return 'cancelled'

                # step_task completed (success or error) — cancel the wait task.
                cancel_wait_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cancel_wait_task

                try:
                    result = step_task.result()
                except Exception as exc:  # noqa: BLE001 # allowed-broad: pipeline boundary
                    error_str = str(exc)
                    logs.emit_safe(  # allowed-emit-safe: observability
                        level=LogLevel.WARNING,
                        message='Runner step failed',
                        component=_COMPONENT,
                        payload=merge_emit_component_trace_fields(
                            {
                                'run_id': str(run_id),
                                'step_run_id': str(step_run_id),
                                'error': error_str,
                            },
                            component_id=_COMPONENT,
                            target_id=str(run_id),
                        ),
                        correlation_id=correlation_id,
                    )
                    # Roll back session_b (action's in-flight state).
                    await session_b.rollback()

                    # Session C: persist failure — step_run exists in DB (committed above).
                    async with session_factory() as session_c:
                        svc_c = PipelineOrchestratorService(session=session_c, events=events, logs=logs)
                        await svc_c.mark_step_failed(
                            step_run_id,
                            error=error_str,
                            correlation_id=correlation_id,
                        )
                        await svc_c.mark_pipeline_failed(
                            run_id,
                            error=f'step {step_name!r} failed: {error_str}',
                            correlation_id=correlation_id,
                        )
                        await session_c.commit()

                    return 'failed'

                # Action succeeded — persist step result.
                svc_b = PipelineOrchestratorService(session=session_b, events=events, logs=logs)
                await svc_b.mark_step_succeeded(step_run_id, result, correlation_id=correlation_id)
                await session_b.commit()
        finally:
            _hb_stop.set()
            try:
                await _hb_task
            except asyncio.CancelledError:
                pass  # defensive — stop_event is the normal exit signal

        step_results[step_name] = {'result': result}

    # All steps done — complete the run.
    async with session_factory() as session_b:
        svc_b = PipelineOrchestratorService(session=session_b, events=events, logs=logs)
        await svc_b.mark_pipeline_completed(run_id, correlation_id=correlation_id)
        await session_b.commit()

    logs.emit_safe(  # allowed-emit-safe: observability
        level=LogLevel.INFO,
        message='Runner run completed',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {'run_id': str(run_id)},
            component_id=_COMPONENT,
            target_id=str(run_id),
        ),
        correlation_id=correlation_id,
    )
    return 'completed'


# ---------------------------------------------------------------------------
# _park_wait_for_event — park branch for wait_for_event step kind
# ---------------------------------------------------------------------------


async def _park_wait_for_event(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    run_id: uuid.UUID,
    step: dict[str, Any],
    step_name: str,
    pipeline_args: dict[str, Any],
    step_results: dict[str, dict[str, Any]],
    events: EventService,
    logs: LogService | NoOpLogService,
    correlation_id: str | None,
) -> str:
    """Park the pipeline run on a wait_for_event step.

    Fail-fast order:
    1. Parse timeout (no DB writes on failure).
    2. Resolve templates in match (no DB writes on failure).
    3. create_step_run → mark_step_awaiting_event →
       create_pipeline_event_waiter → mark_pipeline_awaiting_event → commit.

    Returns ``'awaiting_event'`` on success, ``'failed'`` otherwise.
    """
    # 1. Parse timeout — fail before touching the DB.
    raw_timeout: str = step.get('timeout', '')
    try:
        delta = _parse_duration(raw_timeout)
    except ValueError as exc:
        logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.WARNING,
            message='wait_for_event timeout parse failed',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id), 'step_name': step_name, 'raw_timeout': raw_timeout, 'error': str(exc)},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )
        async with session_factory() as session_fail:
            svc_fail = PipelineOrchestratorService(session=session_fail, events=events, logs=logs)
            await svc_fail.mark_pipeline_failed(
                run_id,
                error=f'invalid timeout for step {step_name!r}: {exc}',
                correlation_id=correlation_id,
            )
            await session_fail.commit()
        return 'failed'

    expires_at = datetime.now(UTC) + delta

    # 2. Resolve templates in match — fail before touching the DB.
    raw_match: dict[str, Any] = dict(step.get('match', {}))
    try:
        resolved_match = _resolve_templates(
            raw_match,
            pipeline_args=pipeline_args,
            step_results=step_results,
        )
    except (KeyError, TypeError) as exc:
        async with session_factory() as session_fail:
            svc_fail = PipelineOrchestratorService(session=session_fail, events=events, logs=logs)
            await svc_fail.mark_pipeline_failed(
                run_id,
                error=f'template resolution failed for step {step_name!r}: {exc}',
                correlation_id=correlation_id,
            )
            await session_fail.commit()
        return 'failed'

    resolved_args: dict[str, Any] = {
        'event': step['event'],
        'match': resolved_match,
        'timeout': step['timeout'],
        'on_timeout': step.get('on_timeout', 'fail'),
    }

    # 3. Park sequence — single session, single commit.
    async with session_factory() as session_park:
        svc = PipelineOrchestratorService(session=session_park, events=events, logs=logs)

        step_run = await svc.create_step_run(
            run_id,
            step_name,
            resolved_args,
            correlation_id=correlation_id,
        )
        await svc.mark_step_awaiting_event(step_run.id, correlation_id=correlation_id)
        await svc.create_pipeline_event_waiter(
            step_run.id,
            event_type=step['event'],
            match=resolved_match,
            expires_at=expires_at,
            correlation_id=correlation_id,
        )
        await svc.mark_pipeline_awaiting_event(run_id, correlation_id=correlation_id)
        await session_park.commit()

    logs.emit_safe(  # allowed-emit-safe: observability
        level=LogLevel.DEBUG,
        message='Runner step parking on wait_for_event',
        component=_COMPONENT,
        payload=merge_emit_component_trace_fields(
            {
                'run_id': str(run_id),
                'step_run_id': str(step_run.id),
                'event_type': step['event'],
                'expires_at': expires_at.isoformat(),
            },
            component_id=_COMPONENT,
            target_id=str(run_id),
        ),
        correlation_id=correlation_id,
    )
    return 'awaiting_event'


# ---------------------------------------------------------------------------
# work_loop
# ---------------------------------------------------------------------------


async def work_loop(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    pipeline_loader: Any,
    events: EventService,
    logs: LogService | NoOpLogService,
    slot_index: int,
    shutdown_event: asyncio.Event,
    poll_interval: float = 1.0,
    drain_timeout: float = 60.0,
) -> None:
    """Main work loop for one executor slot.

    Runs until ``shutdown_event`` is set.  On idle, waits up to ``poll_interval``
    seconds before trying again — respects the shutdown signal during the wait.

    Each tick starts with ``reclaim_sweep_tick`` to release stale runs from dead
    workers before attempting to claim new work.

    On shutdown (``shutdown_event`` set after a tick):
    - If a run is currently in-flight, ``drain_active_run`` waits up to
      ``drain_timeout`` seconds for it to finish naturally, then self-releases.
    - If idle, exits immediately.

    Note: ``drain_timeout`` must be > ``_RECLAIM_STALE_THRESHOLD_SECONDS`` (10s)
    so that the row goes stale before self-release is attempted.  The caller
    (platform_executor_node/main.py) clamps the value to ensure this invariant.
    """
    worker = WorkerIdentity.create(slot_index=slot_index)
    run_handle = RunHandle()

    while not shutdown_event.is_set():
        # Sweep stale runs before attempting to claim new work.
        try:
            await reclaim_sweep_tick(session_factory, events=events, logs=logs)
        except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
            logs.emit_safe(  # allowed-emit-safe: observability
                level=LogLevel.WARNING,
                message='Reclaim sweep tick raised',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'error': str(exc)},
                    component_id=_COMPONENT,
                    target_id='runner',
                ),
            )

        try:
            outcome = await run_one_iteration(
                session_factory,
                worker=worker,
                pipeline_loader=pipeline_loader,
                events=events,
                logs=logs,
                run_handle=run_handle,
            )
        except Exception as exc:  # noqa: BLE001 # allowed-broad: task-loop guard
            logs.emit_safe(  # allowed-emit-safe: observability
                level=LogLevel.ERROR,
                message='Runner work loop tick failed',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'error': str(exc)},
                    component_id=_COMPONENT,
                    target_id='runner',
                ),
            )
            outcome = 'idle'

        if outcome == 'idle':
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=poll_interval)
            except TimeoutError:
                pass

    # Shutdown: drain in-flight run if any.
    if run_handle.run_id is not None:
        await drain_active_run(
            session_factory,
            run_id=run_handle.run_id,
            completion_event=run_handle.completion,
            events=events,
            logs=logs,
            drain_timeout=drain_timeout,
        )

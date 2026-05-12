# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""PipelineOrchestratorService — sole writer for pipeline_runs, step_runs,
pipeline_event_waiters.

Invariants enforced here:
- Pipeline state-ownership: only this service writes to the three tables.
  (ARCH_CONTEXT.md lines 360-363)
- Trigger idempotency: content-hash dedupe through the partial UNIQUE index.
  (ARCH_CONTEXT.md lines 392-396)

Design notes:
- All methods accept an AsyncSession; the service does NOT commit.
  The caller (test, runner, REST handler) owns the transaction boundary.
- EventService.emit re-raises on failure — domain-event publish failures
  propagate and roll back the caller's transaction.  This is intentional
  (event-sourced audit integrity, parity with reconciliation/sync_apply).
- cancel-vs-complete race: if mark_pipeline_completed/failed finds the row
  already in 'cancelling', it transitions it to 'cancelled' silently and
  does NOT emit pipeline.run.completed.  Step 18 owns the cancel event.

Routing keys emitted (8):
  pipeline.run.created, pipeline.run.started, pipeline.run.completed,
  pipeline.run.failed, pipeline.run.heartbeat_lost, pipeline.run.cancelled,
  pipeline.step.started, pipeline.step.completed, pipeline.step.failed,
  pipeline.step.aborted.
"""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
from typing import TYPE_CHECKING, Any, cast
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine.cursor import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.events.schemas import EventEnvelope, EventParticipantKind, new_event_envelope
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_component_trace_fields
from src.platform.orchestrator.models import (
    PipelineEventWaiter,
    PipelineEventWaiterStatus,
    PipelineRun,
    PipelineRunStatus,
    PipelineTriggerSource,
    StepRun,
    StepRunStatus,
)
from src.platform.orchestrator.service_types import (
    AlreadyCancellingError,
    CancelOutcome,
    OrchestratorRowMissing,
    OrchestratorStateConflict,
    PipelineRunCreateResult,
    ReclaimResult,
    RunNotRetryableError,
    TerminalStatusError,
)

if TYPE_CHECKING:
    from src.platform.events.service import EventService

_COMPONENT = 'pipeline_orchestrator'

# Hard-coded reclaim threshold (Phase 18).  A run is considered stale when its
# last_heartbeat_at is older than this many seconds.  Move to RuntimeSettings
# post-Phase 18.
_RECLAIM_STALE_THRESHOLD_SECONDS = 10.0

# Hard-coded expiry sweep batch size (Phase 18 Step 16).  Move to
# RuntimeSettings together with all other orchestrator tunables post-Phase 18.
_EXPIRY_SWEEP_BATCH_SIZE = 100


# ---------------------------------------------------------------------------
# Module-level helper
# ---------------------------------------------------------------------------


def compute_content_hash(args: dict[str, Any]) -> str:
    """Return sha256(canonical_json(args)) as a hex string.

    Kept as a module-level free function so routes.py (Step 11) can reuse
    it without importing the service class.
    """
    canonical = json.dumps(args, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Event envelope builders
# ---------------------------------------------------------------------------


def _envelope(
    event_type: str,
    payload: dict[str, Any],
    *,
    run_id: uuid.UUID,
    correlation_id: str | None,
) -> EventEnvelope:
    cid = correlation_id if correlation_id is not None else uuid.uuid4().hex
    return new_event_envelope(
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        correlation_id=cid,
        payload={'run_id': str(run_id), **payload},
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.COMPONENT,
        target_id=_COMPONENT,
    )


def _run_created_event(run: PipelineRun, correlation_id: str | None) -> EventEnvelope:
    return _envelope(
        'pipeline.run.created',
        {'pipeline_name': run.pipeline_name, 'pipeline_version': run.pipeline_version},
        run_id=run.id,
        correlation_id=correlation_id,
    )


def _run_started_event(run_id: uuid.UUID, correlation_id: str | None) -> EventEnvelope:
    return _envelope('pipeline.run.started', {}, run_id=run_id, correlation_id=correlation_id)


def _run_completed_event(run_id: uuid.UUID, correlation_id: str | None) -> EventEnvelope:
    return _envelope('pipeline.run.completed', {}, run_id=run_id, correlation_id=correlation_id)


def _run_failed_event(run_id: uuid.UUID, error: str, correlation_id: str | None) -> EventEnvelope:
    return _envelope('pipeline.run.failed', {'error': error}, run_id=run_id, correlation_id=correlation_id)


def _step_started_event(
    run_id: uuid.UUID,
    step_run_id: uuid.UUID,
    step_name: str,
    correlation_id: str | None,
) -> EventEnvelope:
    return _envelope(
        'pipeline.step.started',
        {'step_run_id': str(step_run_id), 'step_name': step_name},
        run_id=run_id,
        correlation_id=correlation_id,
    )


def _step_completed_event(
    run_id: uuid.UUID,
    step_run_id: uuid.UUID,
    step_name: str,
    result: dict[str, Any] | None,
    correlation_id: str | None,
) -> EventEnvelope:
    return _envelope(
        'pipeline.step.completed',
        {'step_run_id': str(step_run_id), 'step_name': step_name, 'result': result},
        run_id=run_id,
        correlation_id=correlation_id,
    )


def _step_failed_event(
    run_id: uuid.UUID,
    step_run_id: uuid.UUID,
    step_name: str,
    error: str,
    correlation_id: str | None,
) -> EventEnvelope:
    return _envelope(
        'pipeline.step.failed',
        {'step_run_id': str(step_run_id), 'step_name': step_name, 'error': error},
        run_id=run_id,
        correlation_id=correlation_id,
    )


def _run_heartbeat_lost_event(
    run_id: uuid.UUID,
    previous_worker_id: str | None,
    stale_for_seconds: float | None,
    correlation_id: str | None,
) -> EventEnvelope:
    """Event emitted when a stale pipeline run is reclaimed.

    ``previous_worker_id`` is a ``<host>-<pid>-<slot>`` string — not PII.
    ``stale_for_seconds`` is the approximate staleness duration for observability.
    """
    payload: dict[str, object] = {'previous_worker_id': previous_worker_id}
    if stale_for_seconds is not None:
        payload['stale_for_seconds'] = stale_for_seconds
    return _envelope(
        'pipeline.run.heartbeat_lost',
        payload,
        run_id=run_id,
        correlation_id=correlation_id,
    )


def _step_aborted_event(
    run_id: uuid.UUID,
    step_run_id: uuid.UUID,
    step_name: str,
    attempt: int,
    reason: str,
    correlation_id: str | None,
) -> EventEnvelope:
    """Event emitted when a step attempt is aborted by the reclaim transaction."""
    return _envelope(
        'pipeline.step.aborted',
        {
            'step_run_id': str(step_run_id),
            'step_name': step_name,
            'attempt': attempt,
            'reason': reason,
        },
        run_id=run_id,
        correlation_id=correlation_id,
    )


def _run_cancelled_event(
    run_id: uuid.UUID,
    previous_status: str,
    correlation_id: str | None,
) -> EventEnvelope:
    """Event emitted when a pipeline run is cancelled.

    Routing key: ``pipeline.run.cancelled``.
    ``previous_status`` is the status the run was in before cancellation was
    initiated — one of ``pending``, ``running``, ``awaiting_event``, ``cancelling``.
    No PII in payload.
    """
    return _envelope(
        'pipeline.run.cancelled',
        {'previous_status': previous_status},
        run_id=run_id,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------------------
# Internal query helpers
# ---------------------------------------------------------------------------


async def _select_run(session: AsyncSession, run_id: uuid.UUID) -> PipelineRun | None:
    result = await session.execute(sa.select(PipelineRun).where(PipelineRun.id == run_id))
    return result.scalar_one_or_none()


async def _guarded_update(
    session: AsyncSession,
    stmt: sa.Update,
) -> int:
    """Execute a status-guarded UPDATE and return the affected row count."""
    result = cast(CursorResult[Any], await session.execute(stmt.execution_options(synchronize_session=False)))
    return result.rowcount


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PipelineOrchestratorService:
    """State-ownership writer for the three orchestrator tables.

    Constructor parameters are keyword-only.  No defaults — all deps required.
    Caller owns commit/rollback.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        events: EventService,
        logs: LogService | NoOpLogService,
    ) -> None:
        self._session = session
        self._events = events
        self._logs: LogService | NoOpLogService = logs

    # ------------------------------------------------------------------
    # PipelineRun lifecycle
    # ------------------------------------------------------------------

    async def create_pipeline_run(
        self,
        pipeline_name: str,
        pipeline_version: int,
        args: dict[str, Any],
        trigger_source: PipelineTriggerSource,
        *,
        retry_of_run_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
    ) -> PipelineRunCreateResult:
        """Insert a new pipeline run row.

        When ``retry_of_run_id`` is None and a duplicate in-flight row exists
        (partial-UNIQUE hit), returns the existing row with ``created=False``.
        When ``retry_of_run_id`` is not None, retries bypass the UNIQUE and
        always insert a fresh row.

        Emits ``pipeline.run.created`` only on a genuine insert.
        """
        content_hash = compute_content_hash(args)

        if retry_of_run_id is not None:
            run = await self._insert_run(
                pipeline_name, pipeline_version, args, content_hash, trigger_source, retry_of_run_id
            )
            await self._events.emit(_run_created_event(run, correlation_id))
            self._logs.emit_safe(
                level=LogLevel.INFO,
                message='Pipeline run created (retry)',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'run_id': str(run.id), 'retry_of_run_id': str(retry_of_run_id)},
                    component_id=_COMPONENT,
                    target_id=str(run.id),
                ),
                correlation_id=correlation_id,
            )
            return PipelineRunCreateResult(run=run, created=True)

        # Non-retry path: savepoint for partial-UNIQUE dedupe.
        for attempt in range(2):
            try:
                async with self._session.begin_nested():
                    run = await self._insert_run(
                        pipeline_name, pipeline_version, args, content_hash, trigger_source, None
                    )
                # Insert succeeded.
                await self._events.emit(_run_created_event(run, correlation_id))
                self._logs.emit_safe(
                    level=LogLevel.INFO,
                    message='Pipeline run created',
                    component=_COMPONENT,
                    payload=merge_emit_component_trace_fields(
                        {'run_id': str(run.id)},
                        component_id=_COMPONENT,
                        target_id=str(run.id),
                    ),
                    correlation_id=correlation_id,
                )
                return PipelineRunCreateResult(run=run, created=True)

            except IntegrityError as exc:
                if 'uq_pipeline_runs_inflight_idempotency' not in str(exc.orig):
                    raise
                # Savepoint rolled back automatically; outer tx is intact.
                existing = await self._select_inflight_run(pipeline_name, pipeline_version, content_hash)
                if existing is not None:
                    self._logs.emit_safe(
                        level=LogLevel.DEBUG,
                        message='Pipeline run dedupe hit — returning existing in-flight row',
                        component=_COMPONENT,
                        payload=merge_emit_component_trace_fields(
                            {'run_id': str(existing.id)},
                            component_id=_COMPONENT,
                            target_id=str(existing.id),
                        ),
                        correlation_id=correlation_id,
                    )
                    return PipelineRunCreateResult(run=existing, created=False)
                # The conflicting row reached terminal status between INSERT and SELECT.
                # Retry the INSERT once more (attempt 1→2).
                if attempt == 1:
                    raise  # Second conflict — genuine corruption.

        # Should never reach here.
        raise RuntimeError('Unreachable: create_pipeline_run loop exhausted')  # pragma: no cover

    async def create_retry(
        self,
        run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,
    ) -> PipelineRunCreateResult:
        """Create a retry of an existing pipeline run.

        The source run must be in a terminal status (completed, failed, cancelled,
        failed_timeout).  Returns a fresh PipelineRun with retry_of_run_id set.

        Raises:
            OrchestratorRowMissing  — source run not found.
            RunNotRetryableError    — source run is not in a retryable state.
        """
        _NON_TERMINAL = frozenset(
            (
                PipelineRunStatus.pending,
                PipelineRunStatus.running,
                PipelineRunStatus.awaiting_event,
            )
        )

        row = await _select_run(self._session, run_id)
        if row is None:
            raise OrchestratorRowMissing(f'PipelineRun {run_id} not found')

        if row.status == PipelineRunStatus.cancelling:
            raise RunNotRetryableError(run_id, row.status, 'cancelling')

        if row.status in _NON_TERMINAL:
            raise RunNotRetryableError(run_id, row.status, 'non_terminal')

        self._logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.INFO,
            message='Pipeline run retry requested',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id)},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )

        return await self.create_pipeline_run(
            row.pipeline_name,
            row.pipeline_version,
            row.args,
            PipelineTriggerSource.retry,
            retry_of_run_id=row.id,
            correlation_id=correlation_id,
        )

    async def mark_pipeline_running(
        self,
        run_id: uuid.UUID,
        worker_id: str,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Transition pending → running. Writes worker_id into the row."""
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(PipelineRun.id == run_id, PipelineRun.status == PipelineRunStatus.pending)
            .values(
                status=PipelineRunStatus.running,
                started_at=sa.func.now(),
                worker_id=worker_id,
                last_heartbeat_at=sa.func.now(),
            ),
        )
        if n == 0:
            await self._raise_conflict(run_id, (PipelineRunStatus.pending,))
        await self._events.emit(_run_started_event(run_id, correlation_id))
        self._logs.emit_safe(
            level=LogLevel.DEBUG,
            message='Pipeline run marked running',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id), 'worker_id': worker_id},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )

    async def mark_pipeline_awaiting_event(
        self,
        run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Transition running → awaiting_event. Clears worker_id / last_heartbeat_at."""
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(PipelineRun.id == run_id, PipelineRun.status == PipelineRunStatus.running)
            .values(
                status=PipelineRunStatus.awaiting_event,
                worker_id=None,
                last_heartbeat_at=None,
            ),
        )
        if n == 0:
            await self._raise_conflict(run_id, (PipelineRunStatus.running,))
        self._logs.emit_safe(
            level=LogLevel.DEBUG,
            message='Pipeline run marked awaiting_event',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id)},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )

    async def mark_pipeline_running_from_awaiting(
        self,
        run_id: uuid.UUID,
        worker_id: str,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Transition awaiting_event → running. Writes worker_id / last_heartbeat_at."""
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(PipelineRun.id == run_id, PipelineRun.status == PipelineRunStatus.awaiting_event)
            .values(
                status=PipelineRunStatus.running,
                worker_id=worker_id,
                last_heartbeat_at=sa.func.now(),
            ),
        )
        if n == 0:
            await self._raise_conflict(run_id, (PipelineRunStatus.awaiting_event,))
        self._logs.emit_safe(
            level=LogLevel.DEBUG,
            message='Pipeline run marked running from awaiting_event',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id), 'worker_id': worker_id},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )

    async def mark_pipeline_completed(
        self,
        run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Transition running|awaiting_event → completed.

        Cancel-vs-complete race: if the row is already in 'cancelling' when the
        guarded UPDATE misses, the row is transitioned to 'cancelled' instead.
        No pipeline.run.completed is emitted in that case (Step 18 owns the
        cancel event).
        """
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.status.in_((PipelineRunStatus.running, PipelineRunStatus.awaiting_event)),
            )
            .values(status=PipelineRunStatus.completed, finished_at=sa.func.now()),
        )
        if n == 1:
            await self._events.emit(_run_completed_event(run_id, correlation_id))
            return
        await self._handle_complete_or_fail_miss(run_id, error=None, correlation_id=correlation_id)

    async def mark_pipeline_failed(
        self,
        run_id: uuid.UUID,
        error: str,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Transition running|awaiting_event → failed.

        Same cancel-vs-complete race logic as mark_pipeline_completed.
        """
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.status.in_((PipelineRunStatus.running, PipelineRunStatus.awaiting_event)),
            )
            .values(status=PipelineRunStatus.failed, finished_at=sa.func.now(), error=error),
        )
        if n == 1:
            await self._events.emit(_run_failed_event(run_id, error, correlation_id))
            return
        await self._handle_complete_or_fail_miss(run_id, error=error, correlation_id=correlation_id)

    async def mark_pipeline_cancelling(
        self,
        run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Transition pending|running|awaiting_event → cancelling."""
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.status.in_(
                    (
                        PipelineRunStatus.pending,
                        PipelineRunStatus.running,
                        PipelineRunStatus.awaiting_event,
                    )
                ),
            )
            .values(status=PipelineRunStatus.cancelling),
        )
        if n == 0:
            await self._raise_conflict(
                run_id,
                (PipelineRunStatus.pending, PipelineRunStatus.running, PipelineRunStatus.awaiting_event),
            )
        self._logs.emit_safe(
            level=LogLevel.DEBUG,
            message='Pipeline run marked cancelling',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id)},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )

    async def mark_pipeline_cancelled(
        self,
        run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,  # noqa: ARG002
    ) -> None:
        """Transition cancelling|pending → cancelled (terminal).

        'pending' is allowed for the no-runner-yet branch where the run never
        acquired a worker.
        """
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.status.in_((PipelineRunStatus.cancelling, PipelineRunStatus.pending)),
            )
            .values(status=PipelineRunStatus.cancelled, finished_at=sa.func.now()),
        )
        if n == 0:
            await self._raise_conflict(run_id, (PipelineRunStatus.cancelling, PipelineRunStatus.pending))

    # ------------------------------------------------------------------
    # StepRun lifecycle
    # ------------------------------------------------------------------

    async def create_step_run(
        self,
        run_id: uuid.UUID,
        step_name: str,
        args: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> StepRun:
        """Insert attempt=1 StepRun and update pipeline_run.current_step."""
        step = StepRun(
            pipeline_run_id=run_id,
            step_name=step_name,
            attempt=1,
            status=StepRunStatus.running,
            args=args,
            started_at=datetime.now(UTC),
        )
        self._session.add(step)
        await self._session.execute(
            sa.update(PipelineRun).where(PipelineRun.id == run_id).values(current_step=step_name)
        )
        await self._session.flush()
        await self._events.emit(_step_started_event(run_id, step.id, step_name, correlation_id))
        return step

    async def mark_step_succeeded(
        self,
        step_run_id: uuid.UUID,
        result: dict[str, Any] | None,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Transition running → completed for a step attempt."""
        row = await self._require_step(step_run_id)
        n = await _guarded_update(
            self._session,
            sa.update(StepRun)
            .where(StepRun.id == step_run_id, StepRun.status == StepRunStatus.running)
            .values(status=StepRunStatus.completed, result=result, finished_at=sa.func.now()),
        )
        if n == 0:
            self._raise_step_conflict(row.pipeline_run_id, StepRunStatus.running)
        await self._events.emit(
            _step_completed_event(row.pipeline_run_id, step_run_id, row.step_name, result, correlation_id)
        )

    async def mark_step_failed(
        self,
        step_run_id: uuid.UUID,
        error: str,
        *,
        correlation_id: str | None = None,
    ) -> None:
        """Transition running → failed for a step attempt."""
        row = await self._require_step(step_run_id)
        n = await _guarded_update(
            self._session,
            sa.update(StepRun)
            .where(StepRun.id == step_run_id, StepRun.status == StepRunStatus.running)
            .values(status=StepRunStatus.failed, error=error, finished_at=sa.func.now()),
        )
        if n == 0:
            self._raise_step_conflict(row.pipeline_run_id, StepRunStatus.running)
        await self._events.emit(
            _step_failed_event(row.pipeline_run_id, step_run_id, row.step_name, error, correlation_id)
        )

    async def mark_step_awaiting_event(
        self,
        step_run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,  # noqa: ARG002
    ) -> None:
        """Transition running → awaiting_event for a step attempt."""
        row = await self._require_step(step_run_id)
        n = await _guarded_update(
            self._session,
            sa.update(StepRun)
            .where(StepRun.id == step_run_id, StepRun.status == StepRunStatus.running)
            .values(status=StepRunStatus.awaiting_event),
        )
        if n == 0:
            self._raise_step_conflict(row.pipeline_run_id, StepRunStatus.running)

    async def reclaim_step(
        self,
        run_id: uuid.UUID,
        step_name: str,
        *,
        correlation_id: str | None = None,  # noqa: ARG002
    ) -> ReclaimResult:
        """Reclaim a lost step: abort the previous attempt, insert a new one.

        No events are emitted here — Step 13 owns pipeline.run.heartbeat_lost.
        """
        # 1. Lock the latest attempt row.
        q = (
            sa.select(StepRun)
            .where(StepRun.pipeline_run_id == run_id, StepRun.step_name == step_name)
            .order_by(StepRun.attempt.desc())
            .limit(1)
            .with_for_update()
        )
        result = await self._session.execute(q)
        prev = result.scalar_one_or_none()
        if prev is None:
            raise OrchestratorRowMissing(f'No step_run for run={run_id} step={step_name}')

        # 2. Abort previous attempt.
        n = await _guarded_update(
            self._session,
            sa.update(StepRun)
            .where(StepRun.id == prev.id, StepRun.status == StepRunStatus.running)
            .values(
                status=StepRunStatus.aborted,
                error='reclaimed: heartbeat lost',
                finished_at=sa.func.now(),
            ),
        )
        if n == 0:
            self._raise_step_conflict(run_id, StepRunStatus.running)

        # 3. Insert new attempt.
        new_step = StepRun(
            pipeline_run_id=run_id,
            step_name=step_name,
            attempt=prev.attempt + 1,
            status=StepRunStatus.running,
            args=prev.args,
            started_at=datetime.now(UTC),
        )
        self._session.add(new_step)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Another reclaim won — UNIQUE (pipeline_run_id, step_name, attempt) violated.
            raise OrchestratorStateConflict(
                run_id=run_id,
                expected=(PipelineRunStatus.running,),
                actual=None,
            ) from exc

        self._logs.emit_safe(
            level=LogLevel.INFO,
            message='Step reclaimed',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {
                    'run_id': str(run_id),
                    'step_name': step_name,
                    'new_attempt': new_step.attempt,
                },
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
        )
        return ReclaimResult(
            aborted_step_run_id=prev.id,
            new_step_run_id=new_step.id,
            new_attempt=new_step.attempt,
        )

    # ------------------------------------------------------------------
    # Cancel lifecycle
    # ------------------------------------------------------------------

    async def read_status(self, run_id: uuid.UUID) -> PipelineRunStatus | None:
        """Return the current status of a pipeline run, or None if not found.

        Thin SELECT-only helper — no lock, no event, no log.  Used by the runner
        watcher to detect the 'cancelling' flag without coupling to ORM models.
        """
        row = await _select_run(self._session, run_id)
        return row.status if row is not None else None

    async def mark_step_cancelled(
        self,
        step_run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,  # noqa: ARG002
    ) -> None:
        """Transition a step run from running|awaiting_event → cancelled.

        No event emitted — step cancellation is folded into the parent
        pipeline.run.cancelled event (keeps event count low, matches
        existing cancel-vs-complete pattern).
        """
        await _guarded_update(
            self._session,
            sa.update(StepRun)
            .where(
                StepRun.id == step_run_id,
                StepRun.status.in_((StepRunStatus.running, StepRunStatus.awaiting_event)),
            )
            .values(status=StepRunStatus.cancelled, finished_at=sa.func.now()),
        )

    async def request_cancel(
        self,
        run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,
        _depth: int = 0,
    ) -> CancelOutcome:
        """Dispatch cancel by current run status.

        Returns a CancelOutcome describing whether the cancel was synchronous
        (sync=True → status='cancelled') or asynchronous (sync=False →
        status='cancelling', runner watcher owns the terminal transition).

        Raises:
            AlreadyCancellingError — run is already in 'cancelling' status.
            TerminalStatusError    — run is already in a terminal status.
            OrchestratorRowMissing — run not found.
            OrchestratorStateConflict — two consecutive race misses (bounded).
        """
        _TERMINAL = frozenset(
            (
                PipelineRunStatus.completed,
                PipelineRunStatus.failed,
                PipelineRunStatus.cancelled,
                PipelineRunStatus.failed_timeout,
            )
        )

        row = await _select_run(self._session, run_id)
        if row is None:
            raise OrchestratorRowMissing(f'PipelineRun {run_id} not found')

        status = row.status

        if status == PipelineRunStatus.cancelling:
            raise AlreadyCancellingError(run_id)

        if status in _TERMINAL:
            raise TerminalStatusError(run_id, status)

        if status == PipelineRunStatus.pending:
            return await self._cancel_pending(run_id, correlation_id=correlation_id, _depth=_depth)

        if status == PipelineRunStatus.awaiting_event:
            return await self._cancel_awaiting_event(run_id, correlation_id=correlation_id, _depth=_depth)

        # status == running
        return await self._cancel_running(run_id, correlation_id=correlation_id, _depth=_depth)

    async def _cancel_pending(
        self,
        run_id: uuid.UUID,
        *,
        correlation_id: str | None,
        _depth: int,
    ) -> CancelOutcome:
        """Synchronously cancel a pending run."""
        self._logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.INFO,
            message='Pipeline run cancel — pending branch',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id)},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(PipelineRun.id == run_id, PipelineRun.status == PipelineRunStatus.pending)
            .values(status=PipelineRunStatus.cancelled, finished_at=sa.func.now()),
        )
        if n == 0:
            if _depth >= 1:
                raise OrchestratorStateConflict(
                    run_id=run_id,
                    expected=(PipelineRunStatus.pending,),
                    actual=None,
                )
            return await self.request_cancel(run_id, correlation_id=correlation_id, _depth=_depth + 1)

        # Defensive: cancel any step_run rows that might be running (none expected for pending).
        await self._session.execute(
            sa.update(StepRun)
            .where(
                StepRun.pipeline_run_id == run_id,
                StepRun.status == StepRunStatus.running,
            )
            .values(status=StepRunStatus.cancelled, finished_at=sa.func.now())
            .execution_options(synchronize_session=False)
        )

        await self._session.flush()
        await self._events.emit(_run_cancelled_event(run_id, 'pending', correlation_id))
        return CancelOutcome(run_id=run_id, status=PipelineRunStatus.cancelled, sync=True)

    async def _cancel_awaiting_event(
        self,
        run_id: uuid.UUID,
        *,
        correlation_id: str | None,
        _depth: int,
    ) -> CancelOutcome:
        """Synchronously cancel an awaiting_event run."""
        self._logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.INFO,
            message='Pipeline run cancel — awaiting_event branch',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id)},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(PipelineRun.id == run_id, PipelineRun.status == PipelineRunStatus.awaiting_event)
            .values(status=PipelineRunStatus.cancelled, finished_at=sa.func.now()),
        )
        if n == 0:
            if _depth >= 1:
                raise OrchestratorStateConflict(
                    run_id=run_id,
                    expected=(PipelineRunStatus.awaiting_event,),
                    actual=None,
                )
            return await self.request_cancel(run_id, correlation_id=correlation_id, _depth=_depth + 1)

        # Cancel the MAX(attempt) step_run in awaiting_event.
        step_q = (
            sa.select(StepRun)
            .where(
                StepRun.pipeline_run_id == run_id,
                StepRun.status == StepRunStatus.awaiting_event,
            )
            .order_by(StepRun.attempt.desc())
            .limit(1)
        )
        step_result = await self._session.execute(step_q)
        step_row = step_result.scalar_one_or_none()
        if step_row is not None:
            await self.mark_step_cancelled(step_row.id, correlation_id=correlation_id)

        # Delete any pipeline_event_waiters for this step.
        if step_row is not None:
            await self._session.execute(
                sa.delete(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step_row.id)
            )

        await self._session.flush()
        await self._events.emit(_run_cancelled_event(run_id, 'awaiting_event', correlation_id))
        return CancelOutcome(run_id=run_id, status=PipelineRunStatus.cancelled, sync=True)

    async def _cancel_running(
        self,
        run_id: uuid.UUID,
        *,
        correlation_id: str | None,
        _depth: int,
    ) -> CancelOutcome:
        """Asynchronously cancel a running run — transitions to 'cancelling'."""
        self._logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.INFO,
            message='Pipeline run cancel — running branch',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id)},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(PipelineRun.id == run_id, PipelineRun.status == PipelineRunStatus.running)
            .values(status=PipelineRunStatus.cancelling),
        )
        if n == 0:
            if _depth >= 1:
                raise OrchestratorStateConflict(
                    run_id=run_id,
                    expected=(PipelineRunStatus.running,),
                    actual=None,
                )
            return await self.request_cancel(run_id, correlation_id=correlation_id, _depth=_depth + 1)

        return CancelOutcome(run_id=run_id, status=PipelineRunStatus.cancelling, sync=False)

    # ------------------------------------------------------------------
    # PipelineEventWaiter lifecycle
    # ------------------------------------------------------------------

    async def create_pipeline_event_waiter(
        self,
        step_run_id: uuid.UUID,
        event_type: str,
        match: dict[str, Any],
        expires_at: datetime,
        *,
        correlation_id: str | None = None,  # noqa: ARG002
    ) -> PipelineEventWaiter:
        """Insert a waiter row. UNIQUE on step_run_id → second call raises.

        Uses a savepoint so that a UNIQUE violation does not invalidate the
        caller's outer transaction.
        """
        # Pre-fetch run_id while the session is healthy (before savepoint).
        step = await self._session.get(StepRun, step_run_id)
        run_id = step.pipeline_run_id if step else uuid.UUID(int=0)

        waiter = PipelineEventWaiter(
            step_run_id=step_run_id,
            event_type=event_type,
            match=match,
            expires_at=expires_at,
        )
        self._session.add(waiter)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError as exc:
            raise OrchestratorStateConflict(
                run_id=run_id,
                expected=(PipelineRunStatus.awaiting_event,),
                actual=None,
            ) from exc
        return waiter

    async def resolve_pipeline_event_waiter(
        self,
        step_run_id: uuid.UUID,
        event_payload: dict[str, Any],
        *,
        correlation_id: str | None = None,
    ) -> bool:
        """Resolve a waiter: delete it, complete the step, re-activate the run.

        Returns True when this call won the race and the waiter was resolved.
        Returns False when a concurrent call already resolved the waiter (race
        lost — no emit, no state change).

        Concurrency safety:
        - The waiter row is SELECTed with FOR UPDATE so concurrent callers
          serialise; the loser blocks until the winner commits and then
          re-reads a deleted row → raises OrchestratorRowMissing before
          doing any work.
        - The StepRun UPDATE is guarded by ``status='awaiting_event'`` so a
          duplicate delivery that reaches this point after the winner's commit
          hits rowcount=0 and returns False without emitting a second event.
        """
        q = sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step_run_id).with_for_update()
        r = await self._session.execute(q)
        waiter = r.scalar_one_or_none()
        if waiter is None:
            raise OrchestratorRowMissing(f'No event waiter for step_run_id={step_run_id}')

        step = await self._session.get(StepRun, step_run_id)
        if step is None:
            raise OrchestratorRowMissing(f'StepRun {step_run_id} not found')
        run_id = step.pipeline_run_id

        # Delete waiter (already locked above).
        await self._session.execute(
            sa.delete(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step_run_id)
        )

        # Complete the step — guarded by status so a duplicate delivery that
        # races past the FOR UPDATE lock (e.g. after the first winner committed)
        # writes 0 rows and we return False without emitting a second event.
        step_result = cast(
            CursorResult[Any],
            await self._session.execute(
                sa.update(StepRun)
                .where(StepRun.id == step_run_id, StepRun.status == StepRunStatus.awaiting_event)
                .values(status=StepRunStatus.completed, result=event_payload, finished_at=sa.func.now())
            ),
        )
        if step_result.rowcount == 0:
            # Race lost — another concurrent call already completed this step.
            return False

        # Re-activate the run (awaiting_event → pending) so the runner picks it
        # up via SELECT ... FOR UPDATE SKIP LOCKED on the pending queue.
        # Guarded by status='awaiting_event' so a cancelled/cancelling run is
        # unaffected (cancel-vs-resolve race; Step 18 owns the cancel path).
        await self._session.execute(
            sa.update(PipelineRun)
            .where(PipelineRun.id == run_id, PipelineRun.status == PipelineRunStatus.awaiting_event)
            .values(status=PipelineRunStatus.pending)
        )

        await self._events.emit(
            _step_completed_event(run_id, step_run_id, step.step_name, event_payload, correlation_id)
        )
        return True

    async def find_matching_waiter_step_ids(
        self,
        event_type: str,
        payload: dict[str, Any],
    ) -> list[uuid.UUID]:
        """Return step_run_ids of waiting waiters whose match JSONB is contained in payload.

        Uses Postgres JSONB containment operator ``<@`` (match is a subset of payload).
        Only rows with status='waiting' are eligible — matched/expired/cancelled rows
        are forensic and MUST NOT re-fire.
        """
        stmt = sa.select(PipelineEventWaiter.step_run_id).where(
            PipelineEventWaiter.event_type == event_type,
            PipelineEventWaiter.status == PipelineEventWaiterStatus.waiting,
            PipelineEventWaiter.match.op('<@')(sa.cast(sa.bindparam('p', type_=JSONB), JSONB)),
        )
        result = await self._session.execute(stmt, {'p': payload})
        return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _insert_run(
        self,
        pipeline_name: str,
        pipeline_version: int,
        args: dict[str, Any],
        content_hash: str,
        trigger_source: PipelineTriggerSource,
        retry_of_run_id: uuid.UUID | None,
    ) -> PipelineRun:
        run = PipelineRun(
            pipeline_name=pipeline_name,
            pipeline_version=pipeline_version,
            args=args,
            content_hash=content_hash,
            status=PipelineRunStatus.pending,
            trigger_source=trigger_source,
            retry_of_run_id=retry_of_run_id,
        )
        self._session.add(run)
        await self._session.flush()
        return run

    async def _select_inflight_run(
        self,
        pipeline_name: str,
        pipeline_version: int,
        content_hash: str,
    ) -> PipelineRun | None:
        q = sa.select(PipelineRun).where(
            PipelineRun.pipeline_name == pipeline_name,
            PipelineRun.pipeline_version == pipeline_version,
            PipelineRun.content_hash == content_hash,
            PipelineRun.retry_of_run_id.is_(None),
            PipelineRun.status.in_(
                (
                    PipelineRunStatus.pending,
                    PipelineRunStatus.running,
                    PipelineRunStatus.awaiting_event,
                    PipelineRunStatus.cancelling,
                )
            ),
        )
        result = await self._session.execute(q)
        return result.scalar_one_or_none()

    async def _raise_conflict(
        self,
        run_id: uuid.UUID,
        expected: tuple[PipelineRunStatus, ...],
    ) -> None:
        row = await _select_run(self._session, run_id)
        actual = row.status if row else None
        self._logs.emit_safe(
            level=LogLevel.DEBUG,
            message='Orchestrator state conflict',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {
                    'run_id': str(run_id),
                    'expected': [s.value for s in expected],
                    'actual': actual.value if actual else None,
                },
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
        )
        raise OrchestratorStateConflict(run_id=run_id, expected=expected, actual=actual)

    def _raise_step_conflict(self, run_id: uuid.UUID, expected_step_status: StepRunStatus) -> None:
        """Raise OrchestratorStateConflict for a step-level guard miss.

        Reuses PipelineRunStatus as the ``expected`` type slot for type
        consistency; callers interpret the value from the error message.
        """
        raise OrchestratorStateConflict(
            run_id=run_id,
            expected=(PipelineRunStatus.running,),
            actual=None,
        )

    async def _handle_complete_or_fail_miss(
        self,
        run_id: uuid.UUID,
        error: str | None,
        correlation_id: str | None,
    ) -> None:
        """Handle the 0-rowcount case for mark_pipeline_completed/failed.

        If the row is in 'cancelling', silently transition it to 'cancelled'
        (cancel-vs-complete race).  No event emitted for the cancel case.
        Otherwise raises OrchestratorStateConflict.
        """
        row = await _select_run(self._session, run_id)
        if row is None:
            raise OrchestratorRowMissing(f'PipelineRun {run_id} not found')

        if row.status == PipelineRunStatus.cancelling:
            n = await _guarded_update(
                self._session,
                sa.update(PipelineRun)
                .where(PipelineRun.id == run_id, PipelineRun.status == PipelineRunStatus.cancelling)
                .values(status=PipelineRunStatus.cancelled, finished_at=sa.func.now()),
            )
            if n == 0:
                raise OrchestratorStateConflict(
                    run_id=run_id,
                    expected=(PipelineRunStatus.cancelling,),
                    actual=None,
                )
            # Step 18 owns pipeline.run.cancelled — no emit here.
            self._logs.emit_safe(
                level=LogLevel.DEBUG,
                message='Pipeline run cancelled during complete/fail (race)',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {'run_id': str(run_id), 'error': error},
                    component_id=_COMPONENT,
                    target_id=str(run_id),
                ),
                correlation_id=correlation_id,
            )
            return

        raise OrchestratorStateConflict(
            run_id=run_id,
            expected=(PipelineRunStatus.running, PipelineRunStatus.awaiting_event),
            actual=row.status,
        )

    async def claim_pending_run(
        self,
        worker_id: str,
        *,
        correlation_id: str | None = None,
    ) -> PipelineRun | None:
        """Atomically claim a pending pipeline run for the given worker.

        Uses SELECT ... FOR UPDATE SKIP LOCKED to ensure at-most-one delivery:
        two concurrent callers cannot claim the same row.

        Returns the claimed and refreshed PipelineRun, or None if no pending
        run is available (empty queue or all rows locked by peers).

        Step 1 — find oldest unclaimed pending row.
        Step 2 — guarded UPDATE: status=running + worker_id + timestamps.
                  If 0 rows affected (status changed between SELECT and UPDATE),
                  return None (lost the race).
        Step 3 — emit pipeline.run.started.
        Step 4 — return refreshed ORM row.
        """
        # Step 1: SELECT the candidate row ID (FOR UPDATE SKIP LOCKED).
        q = (
            sa.select(PipelineRun.id)
            .where(
                PipelineRun.status == PipelineRunStatus.pending,
            )
            .order_by(PipelineRun.created_at.asc())
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        result = await self._session.execute(q)
        row_id: uuid.UUID | None = result.scalar_one_or_none()
        if row_id is None:
            return None

        # Step 2: Guarded UPDATE — only succeeds if status is still 'pending'.
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(PipelineRun.id == row_id, PipelineRun.status == PipelineRunStatus.pending)
            .values(
                status=PipelineRunStatus.running,
                worker_id=worker_id,
                started_at=sa.func.now(),
                last_heartbeat_at=sa.func.now(),
            ),
        )
        if n == 0:
            # Lost the race (status changed between SELECT and UPDATE).
            return None

        # Step 3: Emit domain event.
        await self._events.emit(_run_started_event(row_id, correlation_id))

        # Step 4: Log and return refreshed row.
        self._logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.DEBUG,
            message='Pipeline run claimed',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(row_id), 'worker_id': worker_id},
                component_id=_COMPONENT,
                target_id=str(row_id),
            ),
            correlation_id=correlation_id,
        )
        return await _select_run(self._session, row_id)

    async def refresh_heartbeat(
        self,
        run_id: uuid.UUID,
        worker_id: str,
    ) -> bool:
        """Refresh ``pipeline_runs.last_heartbeat_at`` for a live worker.

        Performs a status-guarded UPDATE that sets ``last_heartbeat_at = now()``
        only when the row is still owned by *worker_id* and is in ``running``
        status.  Returns ``True`` if the row was updated, ``False`` otherwise
        (wrong worker, wrong status, or row gone).

        This is the only writer method on this service that does NOT emit a
        domain event.  Refreshing ``last_heartbeat_at`` is a liveness signal,
        not a state transition, so it is deliberately exempt from the
        event-emission invariant documented at the top of this module.

        Detects dead processes, not hung actions; action-level timeouts are
        out of scope.

        Caller owns commit.  No ``correlation_id`` parameter — this is a
        liveness write, not a domain transition; it must not appear in audit
        trails.
        """
        n = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.worker_id == worker_id,
                PipelineRun.status == PipelineRunStatus.running,
            )
            .values(last_heartbeat_at=sa.func.now()),
        )
        return n > 0

    async def reclaim_stale_run(
        self,
        run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,
    ) -> bool:
        """Atomically release ownership of a stale pipeline run.

        A run is considered stale when it is in ``running`` status and its
        ``last_heartbeat_at`` is older than ``_RECLAIM_STALE_THRESHOLD_SECONDS``.

        Single transaction (caller commits):
        1. Guarded UPDATE: status → pending, worker_id/last_heartbeat_at/started_at → NULL.
           Captures ``previous_worker_id`` via RETURNING.  Returns ``False`` on 0 rows
           (row already reclaimed, fresh heartbeat, or wrong status).
        2. SELECT … FOR UPDATE the latest running StepRun (if any); mark it ``aborted``.
        3. flush (pre-commit sentinel).
        4. Emit ``pipeline.run.heartbeat_lost`` (always on success).
        5. Emit ``pipeline.step.aborted`` if a StepRun was aborted.
        6. emit_safe INFO log.
        7. Return ``True``.

        Idempotent: a second call hits ``status='pending'`` → rowcount=0 → ``False``,
        no duplicate events.
        """
        # Step 1: SELECT the current row values (FOR UPDATE) to capture
        # previous_worker_id before we null it out; then issue the guarded UPDATE.
        # Two-statement approach keeps us from needing RETURNING on UPDATE which
        # would give post-update (already nulled) values.
        lock_q = (
            sa.select(PipelineRun.worker_id, PipelineRun.last_heartbeat_at)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.status == PipelineRunStatus.running,
                sa.or_(
                    PipelineRun.last_heartbeat_at.is_(None),
                    PipelineRun.last_heartbeat_at
                    < sa.func.now() - sa.text(f"interval '{_RECLAIM_STALE_THRESHOLD_SECONDS} seconds'"),
                ),
            )
            .with_for_update()
        )
        lock_result = cast(
            CursorResult[Any],
            await self._session.execute(lock_q.execution_options(synchronize_session=False)),
        )
        lock_row = lock_result.fetchone()
        if lock_row is None:
            return False

        previous_worker_id: str | None = lock_row[0]
        last_heartbeat_at: datetime | None = lock_row[1]
        stale_for_seconds: float | None = None
        if last_heartbeat_at is not None:
            stale_for_seconds = (datetime.now(UTC) - last_heartbeat_at).total_seconds()

        # Guarded UPDATE — now safe because we hold the FOR UPDATE lock.
        stmt = (
            sa.update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                PipelineRun.status == PipelineRunStatus.running,
            )
            .values(
                status=PipelineRunStatus.pending,
                worker_id=None,
                last_heartbeat_at=None,
                started_at=None,
            )
        )
        await self._session.execute(stmt.execution_options(synchronize_session=False))

        # Step 2: find and abort the latest running StepRun.
        step_q = (
            sa.select(StepRun)
            .where(
                StepRun.pipeline_run_id == run_id,
                StepRun.status == StepRunStatus.running,
            )
            .order_by(StepRun.attempt.desc())
            .limit(1)
            .with_for_update()
        )
        step_result = await self._session.execute(step_q)
        step_row = step_result.scalar_one_or_none()

        aborted_step_run_id: uuid.UUID | None = None
        aborted_step_name: str | None = None
        aborted_attempt: int | None = None

        if step_row is not None:
            n_step = await _guarded_update(
                self._session,
                sa.update(StepRun)
                .where(StepRun.id == step_row.id, StepRun.status == StepRunStatus.running)
                .values(
                    status=StepRunStatus.aborted,
                    error='reclaimed: heartbeat lost',
                    finished_at=sa.func.now(),
                ),
            )
            if n_step > 0:
                aborted_step_run_id = step_row.id
                aborted_step_name = step_row.step_name
                aborted_attempt = step_row.attempt

        # Step 3: flush before event emission.
        await self._session.flush()

        # Step 4: emit heartbeat_lost event (always).
        await self._events.emit(
            _run_heartbeat_lost_event(run_id, previous_worker_id, stale_for_seconds, correlation_id)
        )

        # Step 5: emit step.aborted if a StepRun was aborted.
        if aborted_step_run_id is not None:
            await self._events.emit(
                _step_aborted_event(
                    run_id,
                    aborted_step_run_id,
                    aborted_step_name,  # type: ignore[arg-type]
                    aborted_attempt,  # type: ignore[arg-type]
                    'reclaimed: heartbeat lost',
                    correlation_id,
                )
            )

        # Step 6: log.
        log_payload: dict[str, object] = {
            'run_id': str(run_id),
            'previous_worker_id': previous_worker_id,
        }
        if aborted_step_run_id is not None:
            log_payload['aborted_step_run_id'] = str(aborted_step_run_id)
        self._logs.emit_safe(  # allowed-emit-safe: observability
            level=LogLevel.INFO,
            message='Pipeline run reclaimed',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                log_payload,
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )
        return True

    async def list_stale_run_ids(self, *, limit: int = 50) -> list[uuid.UUID]:
        """Return IDs of stale running pipeline runs (no FOR UPDATE — cooperative peek).

        A run is stale when status='running' AND last_heartbeat_at is older than
        ``_RECLAIM_STALE_THRESHOLD_SECONDS``.  Ordered by ``started_at ASC NULLS FIRST``
        so oldest runs are reclaimed first.  The caller must pass each ID to
        ``reclaim_stale_run`` which owns the atomic status guard.
        """
        q = (
            sa.select(PipelineRun.id)
            .where(
                PipelineRun.status == PipelineRunStatus.running,
                sa.or_(
                    PipelineRun.last_heartbeat_at.is_(None),
                    PipelineRun.last_heartbeat_at
                    < sa.func.now() - sa.text(f"interval '{_RECLAIM_STALE_THRESHOLD_SECONDS} seconds'"),
                ),
            )
            .order_by(sa.nullsfirst(PipelineRun.started_at.asc()))
            .limit(limit)
        )
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def list_expired_waiter_step_ids(
        self,
        now: datetime,
        *,
        limit: int = _EXPIRY_SWEEP_BATCH_SIZE,
    ) -> list[uuid.UUID]:
        """Return step_run_ids of waiters whose expires_at is before ``now``.

        Read-only scan (no FOR UPDATE) — the caller passes each id to
        ``expire_event_waiter`` which owns the atomic lock + delete.

        Ordered by expires_at ASC (oldest first) so a bounded batch always
        drains the most overdue waiters before newer ones.
        """
        q = (
            sa.select(PipelineEventWaiter.step_run_id)
            .where(PipelineEventWaiter.expires_at < now)
            .order_by(PipelineEventWaiter.expires_at.asc())
            .limit(limit)
        )
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def expire_event_waiter(
        self,
        step_run_id: uuid.UUID,
        *,
        correlation_id: str | None = None,
    ) -> tuple[bool, uuid.UUID | None]:
        """Atomically expire one event waiter: delete it, fail step + run.

        Returns ``(True, run_id)`` on success.
        Returns ``(False, None)`` when the waiter is already gone (raced by
        matcher or cancel) or the step/run is no longer ``awaiting_event``.

        Caller owns commit.  Idempotent via the FOR UPDATE guard on the
        waiter row.

        Transition: ``awaiting_event`` → ``failed_timeout`` for both
        ``StepRun`` and ``PipelineRun``.  Uses inline guarded UPDATEs (not
        ``mark_step_failed`` / ``mark_pipeline_failed``) because those
        helpers guard on ``status='running'``, but timeout comes from
        ``awaiting_event``.

        Events emitted (reusing existing builders):
        - ``pipeline.step.failed`` with ``error='event_timeout'``
        - ``pipeline.run.failed``  with ``error='event_timeout'``
        """
        # a. Lock waiter row; missing → raced by matcher/cancel → no-op.
        waiter_q = (
            sa.select(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step_run_id).with_for_update()
        )
        waiter_result = await self._session.execute(waiter_q)
        waiter = waiter_result.scalar_one_or_none()
        if waiter is None:
            return (False, None)

        # b. Load StepRun; orphan waiter → delete + no-op (no events).
        step = await self._session.get(StepRun, step_run_id)
        if step is None or step.status != StepRunStatus.awaiting_event:
            await self._session.execute(
                sa.delete(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step_run_id)
            )
            return (False, None)

        run_id = step.pipeline_run_id
        step_name = step.step_name

        # c. Load PipelineRun; cancel already won → delete waiter, no-op.
        run = await _select_run(self._session, run_id)
        if run is None or run.status != PipelineRunStatus.awaiting_event:
            await self._session.execute(
                sa.delete(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step_run_id)
            )
            return (False, None)

        # d. Delete waiter.
        await self._session.execute(
            sa.delete(PipelineEventWaiter).where(PipelineEventWaiter.step_run_id == step_run_id)
        )

        # e. Guarded UPDATE on StepRun: awaiting_event → failed_timeout.
        n_step = await _guarded_update(
            self._session,
            sa.update(StepRun)
            .where(StepRun.id == step_run_id, StepRun.status == StepRunStatus.awaiting_event)
            .values(
                status=StepRunStatus.failed_timeout,
                error='event_timeout',
                finished_at=sa.func.now(),
            ),
        )
        if n_step == 0:
            # Should never happen: we hold the waiter lock + re-checked status.
            raise OrchestratorStateConflict(
                run_id=run_id,
                expected=(PipelineRunStatus.awaiting_event,),
                actual=None,
            )

        # f. Guarded UPDATE on PipelineRun: awaiting_event → failed_timeout.
        n_run = await _guarded_update(
            self._session,
            sa.update(PipelineRun)
            .where(PipelineRun.id == run_id, PipelineRun.status == PipelineRunStatus.awaiting_event)
            .values(
                status=PipelineRunStatus.failed_timeout,
                error='event_timeout',
                finished_at=sa.func.now(),
            ),
        )
        if n_run == 0:
            raise OrchestratorStateConflict(
                run_id=run_id,
                expected=(PipelineRunStatus.awaiting_event,),
                actual=None,
            )

        # g. Flush before event emission.
        await self._session.flush()

        # h. Emit step.failed + run.failed (reuse existing builders).
        await self._events.emit(_step_failed_event(run_id, step_run_id, step_name, 'event_timeout', correlation_id))
        await self._events.emit(_run_failed_event(run_id, 'event_timeout', correlation_id))

        # i. Observability log.
        self._logs.emit_safe(
            level=LogLevel.INFO,
            message='Pipeline event waiter expired',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'run_id': str(run_id), 'step_run_id': str(step_run_id)},
                component_id=_COMPONENT,
                target_id=str(run_id),
            ),
            correlation_id=correlation_id,
        )

        return (True, run_id)

    async def _require_step(self, step_run_id: uuid.UUID) -> StepRun:
        row = await self._session.get(StepRun, step_run_id)
        if row is None:
            raise OrchestratorRowMissing(f'StepRun {step_run_id} not found')
        return row

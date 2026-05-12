# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ReconciliationService — coordinates the reconciliation pipeline with advisory lock.

Design decisions (Step 9):
- ``auto_apply`` runs the pipeline normally; the route delegates apply to
  ``SyncApplyService`` after ``run()`` returns (Step 12).
- Advisory lock via ``pg_try_advisory_lock(bigint)`` on a pinned ``AsyncConnection``
  obtained from ``session.connection()``.  Lock key is derived from ``application_id``
  via ``md5`` to get a deterministic 64-bit bigint.  Released in ``finally``.
- Events are emitted AFTER ``pipeline.run_reconciliation`` returns.  The
  ``run.started`` event carries ``occurred_at = summary.started_at`` so downstream
  consumers can reconstruct causal ordering from timestamps even though the event is
  published after the fact.  This avoids splitting the pipeline just to emit one event
  earlier — a complexity trade-off documented here and acceptable for the current
  throughput target.
- NO writes to ``normalized.access_facts``.
- NO imports of ``AccessFactService`` or ``ArtifactBindingService``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.inventory_reconcile.exceptions import ReconciliationAlreadyRunningError
from src.engines.inventory_reconcile.models import ReconciliationRunStatus
from src.engines.inventory_reconcile.pipeline import run_reconciliation
from src.engines.inventory_reconcile.repository import update_run_status
from src.engines.inventory_reconcile.schemas import ReconciliationRunMode, ReconciliationRunSummary
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.applications.models import Application
from src.platform.events.schemas import EventEnvelope, EventParticipantKind, new_event_envelope
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_component_trace_fields

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog
    from src.platform.events.service import EventService
    from src.platform.lake.config import LakeSettings
    from src.platform.lake.duckdb_session import LakeSession

_COMPONENT = 'engines.inventory_reconcile'

# SQL to derive a deterministic 64-bit advisory lock key from an application UUID.
# Using md5 substring rather than hashtext because hashtext returns int4 (32-bit)
# and has known collisions at low cardinality; md5-truncated-to-16-hex is 64-bit
# and portable across PG versions.
_LOCK_KEY_SQL = text("SELECT ('x' || substr(md5(:app_id), 1, 16))::bit(64)::bigint")
_LOCK_ACQUIRE_SQL = text('SELECT pg_try_advisory_lock(:k)')
_LOCK_RELEASE_SQL = text('SELECT pg_advisory_unlock(:k)')


# ---------------------------------------------------------------------------
# Module-level event builders
# ---------------------------------------------------------------------------


def _build_run_started_event(
    run_id: UUID,
    application_id: UUID,
    started_at: datetime,
    correlation_id: str | None,
) -> EventEnvelope:
    """Build the ``reconciliation.run.started`` envelope."""
    cid = correlation_id if correlation_id is not None else uuid4().hex
    return new_event_envelope(
        event_type='reconciliation.run.started',
        occurred_at=started_at,
        correlation_id=cid,
        payload={
            'run_id': str(run_id),
            'application_id': str(application_id),
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.APPLICATION,
        target_id=str(application_id),
    )


def _build_delta_created_event(
    run_id: UUID,
    application_id: UUID,
    summary: ReconciliationRunSummary,
    correlation_id: str | None,
) -> EventEnvelope:
    """Build the ``reconciliation.delta.created`` envelope."""
    cid = correlation_id if correlation_id is not None else uuid4().hex
    return new_event_envelope(
        event_type='reconciliation.delta.created',
        occurred_at=summary.finished_at,
        correlation_id=cid,
        payload={
            'run_id': str(run_id),
            'application_id': str(application_id),
            'created_count': summary.facts_created,
            'updated_count': summary.facts_updated,
            'revoked_count': summary.facts_revoked,
            'unchanged_count': summary.unchanged_count,
            'observed_snapshot_id': summary.observed_snapshot_id,
            'current_snapshot_id': summary.current_snapshot_id,
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.APPLICATION,
        target_id=str(application_id),
    )


def _build_run_completed_event(
    summary: ReconciliationRunSummary,
    correlation_id: str | None,
) -> EventEnvelope:
    """Build the ``reconciliation.run.completed`` envelope."""
    cid = correlation_id if correlation_id is not None else uuid4().hex
    run_id = summary.run_id
    return new_event_envelope(
        event_type='reconciliation.run.completed',
        occurred_at=summary.finished_at,
        correlation_id=cid,
        payload={
            'run_id': str(run_id) if run_id is not None else None,
            'application_id': str(summary.application_id),
            'started_at': summary.started_at.isoformat(),
            'finished_at': summary.finished_at.isoformat(),
            'facts_created': summary.facts_created,
            'facts_updated': summary.facts_updated,
            'facts_revoked': summary.facts_revoked,
            'unchanged_count': summary.unchanged_count,
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.APPLICATION,
        target_id=str(summary.application_id),
    )


def _build_run_failed_event(
    run_id: UUID,
    application_id: UUID,
    error_message: str,
    correlation_id: str | None,
) -> EventEnvelope:
    """Build the ``reconciliation.run.failed`` envelope."""
    cid = correlation_id if correlation_id is not None else uuid4().hex
    return new_event_envelope(
        event_type='reconciliation.run.failed',
        occurred_at=datetime.now(UTC),
        correlation_id=cid,
        payload={
            'run_id': str(run_id),
            'application_id': str(application_id),
            'error': error_message,
        },
        actor_kind=EventParticipantKind.COMPONENT,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.APPLICATION,
        target_id=str(application_id),
    )


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class ReconciliationService:
    """Orchestrates the reconciliation pipeline with advisory lock and event emission.

    Constructor requires all five dependencies — no defaults.  DI lives in ``deps.py``.
    Tests inject fakes directly.

    Advisory lock protocol:
    1. ``conn = await session.connection()`` — pin a single asyncpg connection.
    2. Compute lock key from md5 of application_id string (deterministic 64-bit bigint).
    3. ``pg_try_advisory_lock`` — non-blocking; ``False`` → ``ReconciliationAlreadyRunningError``.
    4. ``try: ... finally: pg_advisory_unlock`` — released on success AND exception.

    Mode dispatch (in service, not pipeline — pipeline is mode-agnostic):
    - ``auto_apply`` → pipeline runs; status stays ``pending_apply``; route calls
      ``SyncApplyService.apply`` afterwards (Step 12).
    - ``review`` → pipeline runs; status stays ``pending_apply``.
    - ``dry_run`` → pipeline runs; status overridden to ``dry_run_completed`` on success.
    """

    def __init__(
        self,
        *,
        session: AsyncSession,
        lake_session: LakeSession,
        catalog: Catalog,
        events: EventService,
        logs: LogService | NoOpLogService,
        lake_settings: LakeSettings | None = None,
    ) -> None:
        self._session = session
        self._lake_session = lake_session
        self._catalog = catalog
        self._events = events
        self._logs: LogService | NoOpLogService = logs
        self._lake_settings = lake_settings

    async def run(
        self,
        application_id: UUID,
        *,
        mode: ReconciliationRunMode = ReconciliationRunMode.review,
        correlation_id: str | None = None,
    ) -> ReconciliationRunSummary:
        """Run the pipeline and emit domain events.

        Raises:
            ApplicationNotFoundError: when ``application_id`` does not exist.
            ReconciliationAlreadyRunningError: when a lock for this app is already held.
        """
        # --- 1. Application existence check (mode guard removed for auto_apply in Step 12) ---
        if await self._session.get(Application, application_id) is None:
            raise ApplicationNotFoundError(str(application_id))

        # --- 3. Advisory lock on pinned connection ---
        conn = await self._session.connection()

        # Compute deterministic 64-bit lock key
        key_result = await conn.execute(_LOCK_KEY_SQL, {'app_id': str(application_id)})
        lock_key: int = key_result.scalar_one()

        acquired_result = await conn.execute(_LOCK_ACQUIRE_SQL, {'k': lock_key})
        acquired: bool = acquired_result.scalar_one()
        if not acquired:
            raise ReconciliationAlreadyRunningError(application_id)

        try:
            return await self._run_pipeline(
                application_id=application_id,
                mode=mode,
                correlation_id=correlation_id,
                lock_key=lock_key,
                conn=conn,
            )
        finally:
            await conn.execute(_LOCK_RELEASE_SQL, {'k': lock_key})

    async def _run_pipeline(
        self,
        *,
        application_id: UUID,
        mode: ReconciliationRunMode,
        correlation_id: str | None,
        lock_key: int,  # noqa: ARG002 — documented param, may be used for diagnostics later
        conn: object,  # noqa: ARG002 — passed for documentation; lock released by caller
    ) -> ReconciliationRunSummary:
        """Execute pipeline with event/log emission. Called only after lock is held."""
        # Log: run started (best-effort)
        # allowed-emit-safe: observability
        self._logs.emit_safe(
            level=LogLevel.INFO,
            message='Reconciliation run started',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {'application_id': str(application_id)},
                component_id=_COMPONENT,
                target_id=str(application_id),
            ),
            correlation_id=correlation_id,
        )

        run_id: UUID | None = None
        summary: ReconciliationRunSummary | None = None

        batch_size = self._lake_settings.reconciliation_fetch_batch_size if self._lake_settings is not None else 5000

        try:
            summary = await run_reconciliation(
                self._session,
                self._lake_session,
                self._catalog,
                application_id=application_id,
                correlation_id=correlation_id,
                batch_size=batch_size,
                logs=self._logs,
            )
            run_id = summary.run_id

            # Override status for dry_run mode
            if mode == ReconciliationRunMode.dry_run and run_id is not None:
                await update_run_status(
                    self._session,
                    run_id,
                    status=ReconciliationRunStatus.dry_run_completed,
                )

        except Exception as exc:  # noqa: BLE001 # allowed-broad: pipeline boundary
            # Emit run.failed event (load-bearing) + log (best-effort)
            if run_id is not None:
                failed_event = _build_run_failed_event(run_id, application_id, str(exc), correlation_id)
                await self._events.emit(failed_event)

            # allowed-emit-safe: best-effort warning
            self._logs.emit_safe(
                level=LogLevel.ERROR,
                message=f'Reconciliation run failed: {exc}',
                component=_COMPONENT,
                payload=merge_emit_component_trace_fields(
                    {
                        'application_id': str(application_id),
                        'error': str(exc),
                    },
                    component_id=_COMPONENT,
                    target_id=str(application_id),
                ),
                correlation_id=correlation_id,
            )
            raise

        assert summary is not None
        assert run_id is not None

        # Emit events (load-bearing): run.started, delta.created, run.completed
        started_event = _build_run_started_event(run_id, application_id, summary.started_at, correlation_id)
        delta_event = _build_delta_created_event(run_id, application_id, summary, correlation_id)
        completed_event = _build_run_completed_event(summary, correlation_id)

        await self._events.emit(started_event)
        await self._events.emit(delta_event)
        await self._events.emit(completed_event)

        # Log: delta created (best-effort)
        # allowed-emit-safe: observability
        self._logs.emit_safe(
            level=LogLevel.INFO,
            message='Reconciliation delta created',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {
                    'application_id': str(application_id),
                    'run_id': str(run_id),
                    'created_count': summary.facts_created,
                    'updated_count': summary.facts_updated,
                    'revoked_count': summary.facts_revoked,
                    'unchanged_count': summary.unchanged_count,
                },
                component_id=_COMPONENT,
                target_id=str(application_id),
            ),
            correlation_id=correlation_id,
        )

        # Log: run completed (best-effort)
        # allowed-emit-safe: observability
        self._logs.emit_safe(
            level=LogLevel.INFO,
            message='Reconciliation run completed',
            component=_COMPONENT,
            payload=merge_emit_component_trace_fields(
                {
                    'application_id': str(application_id),
                    'run_id': str(run_id),
                },
                component_id=_COMPONENT,
                target_id=str(application_id),
            ),
            correlation_id=correlation_id,
        )

        return summary

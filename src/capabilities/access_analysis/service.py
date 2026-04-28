# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ScanOrchestrationService — slice-root service spanning multiple sub-slices.

This service is the only place that:
  - writes to findings (via the engine)
  - updates scan_runs counters and status after a run
  - emits access_analysis.scan.* and access_analysis.finding.* events

Architecture rules:
  - Only this service emits events (per ARCH_CONTEXT: "Only services emit events").
  - session.commit() is NOT called here — the route handler owns the transaction boundary.
  - Event builders (_build_*_event) are private helpers; never inline EventEnvelope construction.
  - The five event types use the 3-segment format required by EventEnvelope.event_type validator.
"""

from __future__ import annotations

from datetime import UTC, datetime
import os
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis._engine_dedupe import FindingEmission, StatusChangeEmission
from src.capabilities.access_analysis.engine import EngineResult, ScanEngine
from src.capabilities.access_analysis.scan_runs.models import ScanRun, ScanRunStatus, ScanRunTrigger
from src.capabilities.access_analysis.scan_runs.repository import (
    get_scan_run_by_id,
    insert_scan_run,
)
from src.platform.events.factory import event_sink_factory
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, NoOpEventService
from src.platform.lake.config import LakeSettings
from src.platform.lake.duckdb_session import LakeSession, LakeSessionFactory
from src.platform.logs.service import LogService, NoOpLogService

_COMPONENT = 'capabilities.access_analysis'
_ACTOR_KIND = EventParticipantKind.CAPABILITY
_ACTOR_ID = _COMPONENT

# ---------------------------------------------------------------------------
# Event builders (all 3-segment event types per EventEnvelope validator)
# ---------------------------------------------------------------------------


def _build_scan_started_event(
    scan_run: ScanRun,
    correlation_id: str,
    event_id: UUID,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id,
        event_type='access_analysis.scan.started',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            'scan_run_id': scan_run.id,
            'triggered_by': scan_run.triggered_by.value,
            'scope_subject_id': str(scan_run.scope_subject_id) if scan_run.scope_subject_id else None,
            'scope_application_id': str(scan_run.scope_application_id) if scan_run.scope_application_id else None,
        },
        actor_kind=_ACTOR_KIND,
        actor_id=_ACTOR_ID,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(scan_run.id),
    )


def _build_scan_completed_event(
    scan_run: ScanRun,
    correlation_id: str,
    causation_event_id: UUID,
    result: EngineResult,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type='access_analysis.scan.completed',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=causation_event_id,
        payload={
            'scan_run_id': scan_run.id,
            'findings_total': result.findings_total,
            'findings_created_count': len(result.findings_created),
            'findings_reused_count': len(result.findings_reused),
            'findings_by_severity': result.findings_by_severity,
        },
        actor_kind=_ACTOR_KIND,
        actor_id=_ACTOR_ID,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(scan_run.id),
    )


def _build_scan_failed_event(
    scan_run: ScanRun,
    correlation_id: str,
    causation_event_id: UUID,
    error_class: str,
    error_message: str,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type='access_analysis.scan.failed',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=causation_event_id,
        payload={
            'scan_run_id': scan_run.id,
            'error_class': error_class,
            'error_message': error_message,
        },
        actor_kind=_ACTOR_KIND,
        actor_id=_ACTOR_ID,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(scan_run.id),
    )


def _build_finding_created_event(
    emission: FindingEmission,
    correlation_id: str,
    causation_event_id: UUID,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type='access_analysis.finding.created',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=causation_event_id,
        payload={
            'finding_id': emission.finding_id,
            'scan_run_id': emission.scan_run_id,
            'kind': emission.kind.value,
            'severity': emission.severity.value,
            'subject_id': str(emission.subject_id) if emission.subject_id else None,
            'account_id': str(emission.account_id) if emission.account_id else None,
            'rule_id': emission.rule_id,
            'scope_key_id': emission.scope_key_id,
            'scope_value': emission.scope_value,
            'evidence_hash': emission.evidence_hash,
        },
        actor_kind=_ACTOR_KIND,
        actor_id=_ACTOR_ID,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(emission.finding_id),
    )


def _build_finding_status_changed_event(
    sc: StatusChangeEmission,
    scan_run_id: int,
    correlation_id: str,
    causation_event_id: UUID,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid4(),
        event_type='access_analysis.finding.status_changed',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=causation_event_id,
        payload={
            'finding_id': sc.finding_id,
            'scan_run_id': scan_run_id,
            'from_status': sc.from_status.value,
            'to_status': sc.to_status.value,
            'status_reason': sc.status_reason,
            'active_mitigation_id': sc.active_mitigation_id,
        },
        actor_kind=_ACTOR_KIND,
        actor_id=_ACTOR_ID,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(sc.finding_id),
    )


# ---------------------------------------------------------------------------
# EventService factory (slice-local pattern, not get_event_service() platform dep)
# ---------------------------------------------------------------------------


def _build_event_service() -> EventService | NoOpEventService:
    provider = os.environ.get('AURELION_EVENTS_PROVIDER', 'mq')
    try:
        sink = event_sink_factory.get(provider)
        return EventService(sink=sink)
    except Exception:
        return NoOpEventService()


# ---------------------------------------------------------------------------
# ScanOrchestrationService
# ---------------------------------------------------------------------------


class ScanOrchestrationService:
    """Orchestrates full scan runs across all four detection producers.

    Slice-root service — spans scan_runs sub-slice and findings sub-slice.
    The ScanRunService (CRUD) and this service coexist without collision.
    """

    def __init__(
        self,
        session: AsyncSession,
        events: EventService | NoOpEventService | None = None,
        engine: ScanEngine | None = None,
        lake_session: LakeSession | None = None,
        log_service: LogService | NoOpLogService | None = None,
        pg_any_array_max_size: int | None = None,
    ) -> None:
        self._session = session
        self._events = events if events is not None else _build_event_service()
        self._engine = engine if engine is not None else ScanEngine()
        self._lake_session = lake_session
        self._log_service: LogService | NoOpLogService = log_service if log_service is not None else NoOpLogService()
        self._pg_any_array_max_size = (
            pg_any_array_max_size if pg_any_array_max_size is not None else LakeSettings().pg_any_array_max_size
        )

    async def trigger_scan(
        self,
        *,
        triggered_by: ScanRunTrigger,
        scope_subject_id: UUID | None = None,
        scope_application_id: UUID | None = None,
        created_by: str | None = None,
        correlation_id: str | None = None,
    ) -> ScanRun:
        """Create a pending ScanRun row and return it. Does not execute the scan.

        The caller is responsible for then calling run_scan(run.id).
        """
        run = await insert_scan_run(
            self._session,
            triggered_by=triggered_by,
            scope_subject_id=scope_subject_id,
            scope_application_id=scope_application_id,
            created_by=created_by,
        )
        return run

    async def run_scan(
        self,
        scan_run_id: int,
        *,
        correlation_id: str | None = None,
    ) -> ScanRun:
        """Execute one created scan run end-to-end.

        Flow:
          1. Load and validate the ScanRun.
          2. Capture at=now(UTC) uniformly for the whole run.
          3. Transition status=running, emit scan.started.
          4. Delegate to ScanEngine.run().
          5. On success: update counters + status=completed, emit finding events + scan.completed.
          6. On failure: update status=failed + error_message, emit scan.failed.
          7. Flush (no commit — route handler owns the boundary).

        Raises ScanRunNotFoundError when the scan_run_id is missing (let route translate).
        Returns the updated ScanRun.
        """
        from src.capabilities.access_analysis.scan_runs.exceptions import (
            ScanRunNotFoundError,
            ScanRunStatusTransitionError,
        )

        scan_run = await get_scan_run_by_id(self._session, scan_run_id)
        if scan_run is None:
            raise ScanRunNotFoundError(scan_run_id)

        if scan_run.status != ScanRunStatus.pending:
            raise ScanRunStatusTransitionError(scan_run.status, ScanRunStatus.running)

        # Capture at once — used for all capability grant / mitigation filtering and Finding.evaluated_at
        at = datetime.now(UTC)

        # Build correlation_id
        run_correlation_id = correlation_id if correlation_id is not None else uuid4().hex

        # Transition to running
        scan_run.status = ScanRunStatus.running
        scan_run.started_at = at
        await self._session.flush()

        # Emit scan.started — root event, causation_id=None
        started_event_id = uuid4()
        started_event = _build_scan_started_event(scan_run, run_correlation_id, started_event_id)
        await self._events.emit(started_event)

        # Run the engine — passes lake_session, log_service, pg_any_array_max_size
        # acquired by the route handler via DI and forwarded through the orchestrator.
        # lake_session must be provided by the caller. If None, the engine will fail
        # gracefully (result.error is set) rather than raising, so scan transitions to failed.
        effective_lake_session = self._lake_session
        if effective_lake_session is None:
            # Minimal DuckDB session for legacy/test contexts where no lake is configured.
            # iceberg_scan will return 0 rows (no warehouse), unused detector finds nothing.
            _tmp_factory = LakeSessionFactory(
                settings=LakeSettings(),
                log_service=self._log_service,  # type: ignore[arg-type]
                pg_dsn=None,
            )
            effective_lake_session = _tmp_factory.acquire()
            _owned_session = effective_lake_session
        else:
            _owned_session = None

        result: EngineResult = await self._engine.run(
            self._session,
            scan_run,
            at=at,
            correlation_id=run_correlation_id,
            lake_session=effective_lake_session,
            log_service=self._log_service,  # type: ignore[arg-type]
            pg_any_array_max_size=self._pg_any_array_max_size,
        )

        if _owned_session is not None:
            _owned_session.__exit__(None, None, None)

        if result.error is not None:
            # Engine failed
            scan_run.status = ScanRunStatus.failed
            scan_run.completed_at = datetime.now(UTC)
            scan_run.error_message = result.error.error_message
            await self._session.flush()

            failed_event = _build_scan_failed_event(
                scan_run,
                run_correlation_id,
                started_event_id,
                result.error.error_class,
                result.error.error_message,
            )
            await self._events.emit(failed_event)
            return scan_run

        # Success — emit finding events, then update ScanRun
        for emission in result.findings_created:
            event = _build_finding_created_event(emission, run_correlation_id, started_event_id)
            await self._events.emit(event)

        for sc in result.findings_status_changed:
            event = _build_finding_status_changed_event(sc, scan_run.id, run_correlation_id, started_event_id)
            await self._events.emit(event)

        # Update ScanRun aggregate columns
        scan_run.status = ScanRunStatus.completed
        scan_run.completed_at = datetime.now(UTC)
        scan_run.findings_total = result.findings_total
        scan_run.findings_by_severity = result.findings_by_severity
        scan_run.findings_created_count = len(result.findings_created)
        scan_run.findings_reused_count = len(result.findings_reused)
        await self._session.flush()

        completed_event = _build_scan_completed_event(scan_run, run_correlation_id, started_event_id, result)
        await self._events.emit(completed_event)

        return scan_run

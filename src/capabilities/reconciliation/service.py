# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ReconciliationService — thin coordinator delegating to pipeline."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.pipeline import run_reconciliation
from src.capabilities.reconciliation.schemas import ReconciliationRunSummary
from src.inventory.access_facts.service import AccessFactService
from src.inventory.artifact_bindings.service import ArtifactBindingService
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.applications.models import Application
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService

_COMPONENT = 'capabilities.reconciliation'


def _build_completed_event(
    summary: ReconciliationRunSummary,
    correlation_id: str | None,
) -> EventEnvelope:
    """Build the reconciliation.run.completed EventEnvelope."""
    cid = correlation_id if correlation_id is not None else uuid4().hex
    level = 'WARNING' if summary.facts_revoked > 100 else 'INFO'
    return EventEnvelope(
        event_id=uuid4(),
        event_type='reconciliation.run.completed',
        occurred_at=datetime.now(UTC),
        correlation_id=cid,
        causation_id=None,
        payload={
            'application_id': str(summary.application_id),
            'started_at': summary.started_at.isoformat(),
            'finished_at': summary.finished_at.isoformat(),
            'artifacts_ingested': summary.artifacts_ingested,
            'facts_created': summary.facts_created,
            'facts_updated': summary.facts_updated,
            'facts_revoked': summary.facts_revoked,
            'artifacts_unhandled': summary.artifacts_unhandled,
            'level': level,
        },
        actor_kind=EventParticipantKind.CAPABILITY,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(summary.application_id),
    )


class ReconciliationService:
    """Orchestrates the artifact-first reconciliation pipeline."""

    def __init__(
        self,
        session: AsyncSession,
        events: EventService | None = None,
        access_fact_service: AccessFactService | None = None,
        artifact_binding_service: ArtifactBindingService | None = None,
        logs: LogService | NoOpLogService | None = None,
    ) -> None:
        self._session = session
        self._events = events if events is not None else noop_event_service
        self._access_fact_service = access_fact_service or AccessFactService()
        self._artifact_binding_service = artifact_binding_service or ArtifactBindingService()
        self._logs: LogService | NoOpLogService = logs if logs is not None else NoOpLogService()

    async def run(
        self,
        application_id: UUID,
        *,
        correlation_id: str | None = None,
    ) -> ReconciliationRunSummary:
        """Run the pipeline and emit the completed event.

        Raises ApplicationNotFoundError when application_id does not exist.
        """
        if await self._session.get(Application, application_id) is None:
            raise ApplicationNotFoundError(str(application_id))

        summary = await run_reconciliation(
            self._session,
            application_id=application_id,
            access_fact_service=self._access_fact_service,
            artifact_binding_service=self._artifact_binding_service,
            correlation_id=correlation_id,
        )
        if summary.facts_errored > 0:
            self._logs.emit_safe(
                level=LogLevel.WARNING,
                message=f'Reconciliation finished with {summary.facts_errored} errored fact(s)',
                component=_COMPONENT,
                payload={
                    'application_id': str(application_id),
                    'facts_errored': summary.facts_errored,
                },
                correlation_id=correlation_id,
            )
        await self.emit_completed(summary, correlation_id)
        return summary

    async def emit_completed(
        self,
        summary: ReconciliationRunSummary,
        correlation_id: str | None,
    ) -> None:
        """Emit reconciliation.run.completed event."""
        event = _build_completed_event(summary, correlation_id)
        await self._events.emit(event)

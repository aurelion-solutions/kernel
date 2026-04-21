# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_artifacts.models import AccessArtifact
from src.inventory.access_artifacts.repository import (
    create_access_artifact as repo_create_access_artifact,
)
from src.inventory.access_artifacts.repository import (
    get_access_artifact_by_id as repo_get_access_artifact_by_id,
)
from src.inventory.access_artifacts.repository import (
    list_access_artifacts as repo_list_access_artifacts,
)
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.access_artifacts'


class AccessArtifactNotFoundError(Exception):
    """Raised when an access artifact is not found."""

    def __init__(self, artifact_id: uuid.UUID) -> None:
        self.artifact_id = artifact_id
        super().__init__(f'Access artifact not found: {artifact_id}')


class AccessArtifactApplicationNotFoundError(Exception):
    """Raised when the referenced application does not exist."""

    def __init__(self, application_id: uuid.UUID) -> None:
        self.application_id = application_id
        super().__init__(f'Application not found: {application_id}')


async def _application_exists(session: AsyncSession, application_id: uuid.UUID) -> bool:
    """Check application existence via ORM model lookup."""
    from src.platform.applications.models import Application

    result = await session.get(Application, application_id)
    return result is not None


class AccessArtifactService:
    """Orchestrates access artifact creation, retrieval, and event emission."""

    def __init__(
        self,
        event_service: EventService | None = None,
    ) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_artifact(
        self,
        session: AsyncSession,
        *,
        application_id: uuid.UUID,
        source_kind: str,
        external_id: str,
        payload: dict[str, Any],
        ingest_batch_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AccessArtifact:
        """Create an access artifact. Validates application existence. Emits inventory.access_artifact.created."""
        if not await _application_exists(session, application_id):
            raise AccessArtifactApplicationNotFoundError(application_id)

        artifact = await repo_create_access_artifact(
            session,
            application_id=application_id,
            source_kind=source_kind,
            external_id=external_id,
            payload=payload,
            ingest_batch_id=ingest_batch_id,
        )

        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.access_artifact.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'artifact_id': str(artifact.id),
                    'application_id': str(application_id),
                    'source_kind': source_kind,
                    'external_id': external_id,
                    'ingest_batch_id': ingest_batch_id,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(artifact.id),
            )
        )
        return artifact

    async def get_artifact(
        self,
        session: AsyncSession,
        artifact_id: uuid.UUID,
    ) -> AccessArtifact | None:
        """Get access artifact by id. No event emitted (Q1 — read-side audit deferred to future audit.* slice)."""
        return await repo_get_access_artifact_by_id(session, artifact_id)

    async def list_artifacts(
        self,
        session: AsyncSession,
        *,
        application_id: uuid.UUID | None = None,
        source_kind: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AccessArtifact]:
        """List access artifacts. No event emitted."""
        return await repo_list_access_artifacts(
            session,
            application_id=application_id,
            source_kind=source_kind,
            limit=limit,
            offset=offset,
        )

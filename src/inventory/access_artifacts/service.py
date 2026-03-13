# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact service — business logic and event emission."""

from __future__ import annotations

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
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service

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
    """Orchestrates access artifact creation, retrieval, and log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def create_artifact(
        self,
        session: AsyncSession,
        *,
        application_id: uuid.UUID,
        source_kind: str,
        external_id: str,
        payload: dict[str, Any],
        ingest_batch_id: str | None = None,
    ) -> AccessArtifact:
        """Create an access artifact. Validates application existence. Emits access_artifact.created."""
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

        self._log.emit_safe(
            'access_artifact.created',
            LogLevel.INFO,
            'Access artifact created',
            _COMPONENT,
            merge_emit_log_participant_fields(
                {
                    'artifact_id': str(artifact.id),
                    'application_id': str(application_id),
                    'source_kind': source_kind,
                    'external_id': external_id,
                    'ingest_batch_id': ingest_batch_id,
                },
                actor_component=_COMPONENT,
                target_id='access_artifact',
            ),
        )
        return artifact

    async def get_artifact(
        self,
        session: AsyncSession,
        artifact_id: uuid.UUID,
    ) -> AccessArtifact | None:
        """Get access artifact by id. Emits access_artifact.retrieved when found."""
        artifact = await repo_get_access_artifact_by_id(session, artifact_id)
        if artifact is not None:
            self._log.emit_safe(
                'access_artifact.retrieved',
                LogLevel.INFO,
                'Access artifact retrieved',
                _COMPONENT,
                merge_emit_log_participant_fields(
                    {'artifact_id': str(artifact_id)},
                    actor_component=_COMPONENT,
                    target_id='access_artifact',
                ),
            )
        return artifact

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

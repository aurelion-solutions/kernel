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
    get_access_artifact_by_id as repo_get_access_artifact_by_id,
)
from src.inventory.access_artifacts.repository import (
    list_access_artifacts as repo_list_access_artifacts,
)
from src.inventory.access_artifacts.repository import (
    tombstone_access_artifact as repo_tombstone_access_artifact,
)
from src.inventory.access_artifacts.repository import (
    upsert_access_artifact as repo_upsert_access_artifact,
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


def _build_access_artifact_ingested_event(
    artifact: AccessArtifact,
    application_id: uuid.UUID,
    artifact_type: str,
    external_id: str,
    ingest_batch_id: str | None,
    correlation_id: str,
    raw_name: str | None,
    effect: str | None,
    valid_from: datetime | None,
    valid_until: datetime | None,
) -> EventEnvelope:
    """Build the inventory.access_artifact.ingested EventEnvelope.

    Payload carries exactly nine keys: artifact_id, application_id, artifact_type,
    external_id, ingest_batch_id (Step 8 shape) plus the four permitted universal
    fields raw_name, effect, valid_from, valid_until (Step 10 extension).
    Timestamps are serialized as ISO-8601 strings; None passes through as None.

    TODO: spec-drift Q4 — phase_12.md §Emitted Events lists 'observed_at' in the
    payload, but Step 8 shipped 'ingest_batch_id' instead. Reconcile in a future step
    once a concrete consumer requires 'observed_at' in the envelope payload.
    """
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.access_artifact.ingested',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            'artifact_id': str(artifact.id),
            'application_id': str(application_id),
            'artifact_type': artifact_type,
            'external_id': external_id,
            'ingest_batch_id': ingest_batch_id,
            'raw_name': raw_name,
            'effect': effect,
            'valid_from': valid_from.isoformat() if valid_from is not None else None,
            'valid_until': valid_until.isoformat() if valid_until is not None else None,
        },
        actor_kind=EventParticipantKind.CAPABILITY,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(artifact.id),
    )


def _build_access_artifact_tombstoned_event(
    artifact: AccessArtifact,
    application_id: uuid.UUID,
    artifact_type: str,
    external_id: str,
    tombstoned_at: datetime,
    observed_at: datetime,
    correlation_id: str,
) -> EventEnvelope:
    """Build the inventory.access_artifact.tombstoned EventEnvelope.

    Payload carries six keys: artifact_id, application_id, artifact_type,
    external_id, tombstoned_at, observed_at — symmetric with .ingested shape.
    Timestamps are ISO-8601 strings.
    """
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='inventory.access_artifact.tombstoned',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            'artifact_id': str(artifact.id),
            'application_id': str(application_id),
            'artifact_type': artifact_type,
            'external_id': external_id,
            'tombstoned_at': tombstoned_at.isoformat(),
            'observed_at': observed_at.isoformat(),
        },
        actor_kind=EventParticipantKind.CAPABILITY,
        actor_id=_COMPONENT,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(artifact.id),
    )


class AccessArtifactService:
    """Orchestrates access artifact upsert, retrieval, and event emission."""

    def __init__(
        self,
        event_service: EventService | None = None,
    ) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def upsert_artifact(
        self,
        session: AsyncSession,
        *,
        application_id: uuid.UUID,
        artifact_type: str,
        external_id: str,
        payload: dict[str, Any],
        ingest_batch_id: str | None = None,
        observed_at: datetime | None = None,
        raw_name: str | None = None,
        effect: str | None = None,
        valid_from: datetime | None = None,
        valid_until: datetime | None = None,
        correlation_id: str | None = None,
    ) -> tuple[AccessArtifact, bool]:
        """Upsert an access artifact on (application_id, artifact_type, external_id).

        Validates application existence before the upsert SQL executes.
        On a fresh INSERT (was_inserted=True): emits inventory.access_artifact.ingested
        with a payload that includes the four permitted universal fields (``raw_name``,
        ``effect``, ``valid_from``, ``valid_until``). Timestamps serialized as ISO-8601.
        On an UPDATE of an existing row (was_inserted=False): emits nothing.

        The four permitted universal fields are forwarded to the repository and refreshed
        on every upsert, same semantics as ``payload``. Passing ``None`` sets the field
        to ``NULL``.

        Returns:
            (artifact, was_inserted) tuple.
        """
        if not await _application_exists(session, application_id):
            raise AccessArtifactApplicationNotFoundError(application_id)

        effective_observed_at = observed_at if observed_at is not None else datetime.now(UTC)

        artifact, was_inserted = await repo_upsert_access_artifact(
            session,
            application_id=application_id,
            artifact_type=artifact_type,
            external_id=external_id,
            payload=payload,
            ingest_batch_id=ingest_batch_id,
            observed_at=effective_observed_at,
            raw_name=raw_name,
            effect=effect,
            valid_from=valid_from,
            valid_until=valid_until,
        )

        if was_inserted:
            effective_correlation_id = correlation_id if correlation_id is not None else uuid.uuid4().hex
            await self._events.emit(
                _build_access_artifact_ingested_event(
                    artifact,
                    application_id,
                    artifact_type,
                    external_id,
                    ingest_batch_id,
                    effective_correlation_id,
                    raw_name,
                    effect,
                    valid_from,
                    valid_until,
                )
            )

        return artifact, was_inserted

    async def tombstone_artifact(
        self,
        session: AsyncSession,
        *,
        artifact_id: uuid.UUID,
        observed_at: datetime | None = None,
        correlation_id: str | None = None,
    ) -> tuple[AccessArtifact, bool]:
        """Tombstone an access artifact: flip is_active false, stamp tombstoned_at.

        Idempotent: calling on an already-tombstoned row returns (artifact, False)
        and emits no event.

        Returns:
            (artifact, True)  — transition happened; inventory.access_artifact.tombstoned emitted.
            (artifact, False) — already tombstoned; no event emitted.

        Raises:
            AccessArtifactNotFoundError: when no row with the given id exists.
        """
        effective_observed_at = observed_at if observed_at is not None else datetime.now(UTC)
        artifact, was_tombstoned = await repo_tombstone_access_artifact(
            session,
            artifact_id=artifact_id,
            observed_at=effective_observed_at,
        )
        if artifact is None:
            raise AccessArtifactNotFoundError(artifact_id)

        if not was_tombstoned:
            # Already inactive — idempotent no-op, no event.
            return artifact, False

        effective_correlation_id = correlation_id if correlation_id is not None else uuid.uuid4().hex
        raw_ts = artifact.tombstoned_at
        tombstoned_at: datetime = raw_ts if isinstance(raw_ts, datetime) else effective_observed_at
        await self._events.emit(
            _build_access_artifact_tombstoned_event(
                artifact,
                artifact.application_id,
                artifact.artifact_type,
                artifact.external_id,
                tombstoned_at,
                effective_observed_at,
                effective_correlation_id,
            )
        )
        return artifact, True

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
        artifact_type: str | None = None,
        is_active: bool | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AccessArtifact]:
        """List access artifacts. No event emitted."""
        return await repo_list_access_artifacts(
            session,
            application_id=application_id,
            artifact_type=artifact_type,
            is_active=is_active,
            limit=limit,
            offset=offset,
        )

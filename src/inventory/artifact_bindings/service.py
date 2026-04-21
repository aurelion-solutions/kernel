# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding service — business logic and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.artifact_bindings.models import ArtifactBinding
from src.inventory.artifact_bindings.repository import (
    create_artifact_binding as repo_create_artifact_binding,
)
from src.inventory.artifact_bindings.repository import (
    get_artifact_binding_by_id as repo_get_artifact_binding_by_id,
)
from src.inventory.artifact_bindings.repository import (
    list_artifact_bindings as repo_list_artifact_bindings,
)
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.artifact_bindings'


class ArtifactBindingTargetRequiredError(Exception):
    """Raised when all three target FKs are None."""


class ArtifactBindingForeignKeyError(Exception):
    """Raised when a referenced entity is not found or FK constraint fails."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ArtifactBindingService:
    """Orchestrates artifact binding creation, retrieval, and event emission."""

    def __init__(
        self,
        event_service: EventService | None = None,
    ) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def create_binding(
        self,
        session: AsyncSession,
        *,
        artifact_id: uuid.UUID,
        access_fact_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        correlation_id: str | None = None,
    ) -> ArtifactBinding:
        """Create an artifact binding. Validates all referenced entities exist. Emits inventory.artifact_binding.created."""  # noqa: E501
        if access_fact_id is None and resource_id is None and account_id is None:
            raise ArtifactBindingTargetRequiredError(
                'At least one of access_fact_id, resource_id, account_id is required'
            )

        from src.inventory.access_artifacts.models import AccessArtifact

        artifact = await session.get(AccessArtifact, artifact_id)
        if artifact is None:
            raise ArtifactBindingForeignKeyError(f'Access artifact not found: {artifact_id}')

        if access_fact_id is not None:
            from src.inventory.access_facts.models import AccessFact

            fact = await session.get(AccessFact, access_fact_id)
            if fact is None:
                raise ArtifactBindingForeignKeyError(f'Access fact not found: {access_fact_id}')

        if resource_id is not None:
            from src.inventory.resources.models import Resource

            resource = await session.get(Resource, resource_id)
            if resource is None:
                raise ArtifactBindingForeignKeyError(f'Resource not found: {resource_id}')

        if account_id is not None:
            from src.inventory.accounts.models import Account

            account = await session.get(Account, account_id)
            if account is None:
                raise ArtifactBindingForeignKeyError(f'Account not found: {account_id}')

        binding = await repo_create_artifact_binding(
            session,
            artifact_id=artifact_id,
            access_fact_id=access_fact_id,
            resource_id=resource_id,
            account_id=account_id,
        )

        await self._events.emit(
            EventEnvelope(
                event_id=uuid.uuid4(),
                event_type='inventory.artifact_binding.created',
                occurred_at=datetime.now(UTC),
                correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                causation_id=None,
                payload={
                    'binding_id': str(binding.id),
                    'artifact_id': str(artifact_id),
                    'access_fact_id': str(access_fact_id) if access_fact_id is not None else None,
                    'resource_id': str(resource_id) if resource_id is not None else None,
                    'account_id': str(account_id) if account_id is not None else None,
                },
                actor_kind=EventParticipantKind.CAPABILITY,
                actor_id=_COMPONENT,
                target_kind=EventParticipantKind.SYSTEM,
                target_id=str(binding.id),
            )
        )
        return binding

    async def get_binding(
        self,
        session: AsyncSession,
        binding_id: uuid.UUID,
    ) -> ArtifactBinding | None:
        """Get artifact binding by id. No event emitted (Q1 — read-side audit deferred to future audit.* slice)."""
        return await repo_get_artifact_binding_by_id(session, binding_id)

    async def list_bindings(
        self,
        session: AsyncSession,
        *,
        artifact_id: uuid.UUID | None = None,
        access_fact_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ArtifactBinding]:
        """List artifact bindings with optional filters. No event emitted."""
        return await repo_list_artifact_bindings(
            session,
            artifact_id=artifact_id,
            access_fact_id=access_fact_id,
            resource_id=resource_id,
            account_id=account_id,
            limit=limit,
            offset=offset,
        )

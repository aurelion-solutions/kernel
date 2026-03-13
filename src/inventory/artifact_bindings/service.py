# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ArtifactBinding service — business logic and operational log emission."""

from __future__ import annotations

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
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service

_COMPONENT = 'inventory.artifact_bindings'


class ArtifactBindingTargetRequiredError(Exception):
    """Raised when all three target FKs are None."""


class ArtifactBindingForeignKeyError(Exception):
    """Raised when a referenced entity is not found or FK constraint fails."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class ArtifactBindingService:
    """Orchestrates artifact binding creation, retrieval, and operational log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def create_binding(
        self,
        session: AsyncSession,
        *,
        artifact_id: uuid.UUID,
        access_fact_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
    ) -> ArtifactBinding:
        """Create an artifact binding. Validates all referenced entities exist."""
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

        self._log.emit_safe(
            'artifact_binding.created',
            LogLevel.INFO,
            'Artifact binding created',
            _COMPONENT,
            merge_emit_log_participant_fields(
                {
                    'binding_id': str(binding.id),
                    'artifact_id': str(artifact_id),
                    'access_fact_id': str(access_fact_id) if access_fact_id is not None else None,
                    'resource_id': str(resource_id) if resource_id is not None else None,
                    'account_id': str(account_id) if account_id is not None else None,
                },
                actor_component=_COMPONENT,
                target_id='artifact_binding',
            ),
        )
        return binding

    async def get_binding(
        self,
        session: AsyncSession,
        binding_id: uuid.UUID,
    ) -> ArtifactBinding | None:
        """Get artifact binding by id. Logs retrieval when found."""
        binding = await repo_get_artifact_binding_by_id(session, binding_id)
        if binding is not None:
            self._log.emit_safe(
                'artifact_binding.retrieved',
                LogLevel.INFO,
                'Artifact binding retrieved',
                _COMPONENT,
                merge_emit_log_participant_fields(
                    {'binding_id': str(binding_id)},
                    actor_component=_COMPONENT,
                    target_id='artifact_binding',
                ),
            )
        return binding

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
        """List artifact bindings with optional filters. No logging."""
        return await repo_list_artifact_bindings(
            session,
            artifact_id=artifact_id,
            access_fact_id=access_fact_id,
            resource_id=resource_id,
            account_id=account_id,
            limit=limit,
            offset=offset,
        )

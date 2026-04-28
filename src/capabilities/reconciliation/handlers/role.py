# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke handler for artifact_type='role'.

Reads payload keys: subject_id, resource_key, resource_type, action_slug,
effect, optional valid_from / valid_until.
Returns a single NormalizationResult after resolving the Resource via
ensure_resource_by_identity.
Invalid payload → [] + DEBUG log (not an exception; engine treats it as
"handler produced nothing").
"""

from __future__ import annotations

from datetime import datetime
import logging
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.reconciliation.contracts import NormalizationResult
from src.capabilities.reconciliation.registry import register_handler
from src.inventory.access_artifacts.schemas import AccessArtifactView
from src.inventory.resources.service import ResourceService

logger = logging.getLogger('reconciliation.handlers.role')


class _RolePayload(BaseModel):
    """Expected shape of AccessArtifact.payload for artifact_type='role'."""

    subject_id: UUID
    resource_key: str
    resource_type: str
    action_slug: str
    effect: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class RoleHandler:
    """Stub handler projecting artifact_type='role' into NormalizationResult.

    Step 14: minimal coverage for pipeline smoke tests only.
    Step 15 will expand to production semantics.
    """

    def __init__(self, resource_service: ResourceService | None = None) -> None:
        self._resource_service = resource_service if resource_service is not None else ResourceService()

    async def handle(
        self,
        artifact: AccessArtifactView,
        session: AsyncSession,
    ) -> list[NormalizationResult]:
        """Parse payload, resolve resource, return single-element list or []."""
        try:
            payload = _RolePayload.model_validate(artifact.payload)
        except ValidationError as exc:
            logger.debug(
                'role handler: invalid payload for artifact %s — %s',
                artifact.id,
                exc,
            )
            return []

        resource = await self._resource_service.ensure_resource_by_identity(
            session,
            application_id=artifact.application_id,
            resource_type=payload.resource_type,
            resource_key=payload.resource_key,
        )

        return [
            NormalizationResult(
                subject_id=payload.subject_id,
                account_id=None,
                resource_id=resource.id,
                action_slug=payload.action_slug,
                effect=payload.effect,
                valid_from=payload.valid_from,
                valid_until=payload.valid_until,
            )
        ]


register_handler('role', RoleHandler())

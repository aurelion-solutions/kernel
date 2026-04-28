# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke handler for artifact_type='sap_role'.

Reads payload keys: subject_id, resource_type, resource_key, action_slug,
effect, optional valid_from / valid_until.
Typically action_slug='use' for role-grants-tcode semantics.
Returns a single NormalizationResult after resolving the Resource via
ensure_resource_by_identity.
Invalid payload → [] + DEBUG log.
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

logger = logging.getLogger('reconciliation.handlers.sap_role')


class _SapRolePayload(BaseModel):
    """Expected shape of AccessArtifact.payload for artifact_type='sap_role'."""

    subject_id: UUID
    resource_type: str
    resource_key: str
    action_slug: str
    effect: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class SapRoleHandler:
    """Smoke handler projecting artifact_type='sap_role' into NormalizationResult.

    Covers the SAP-like class of access encoding: a role grants a subject
    the right to execute a transaction code (tcode) or similar SAP object.
    Step 15: smoke-level coverage — one handler for the SAP class.
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
            payload = _SapRolePayload.model_validate(artifact.payload)
        except ValidationError as exc:
            logger.debug(
                'sap_role handler: invalid payload for artifact %s — %s',
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


register_handler('sap_role', SapRoleHandler())

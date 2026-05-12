# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke handler for artifact_type='privilege'.

Reads payload keys: subject_id, resource_key, resource_type, action_slug,
effect, optional valid_from / valid_until.
Returns a single NormalizationResult after resolving the Resource via
ensure_resource_by_identity.
Invalid payload → [] + DEBUG log.

Intentional duplication of role.py: role and privilege are expected to diverge
in Phase 13+ (SoD semantics, different reconciliation rules). Keeping them
separate from the start avoids a refactor at divergence time.
See phase_12.md §"Role and Privilege Have No Special Status" — the only
difference between role and privilege at Phase 12 level is artifact_type string.
"""

from __future__ import annotations

from datetime import datetime
import logging
from uuid import UUID

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.inventory_reconcile.contracts import NormalizationResult
from src.engines.inventory_reconcile.registry import register_handler
from src.inventory.access_artifacts.schemas import AccessArtifactView
from src.inventory.resources.service import ResourceService

logger = logging.getLogger('reconciliation.handlers.privilege')


class _PrivilegePayload(BaseModel):
    """Expected shape of AccessArtifact.payload for artifact_type='privilege'."""

    subject_id: UUID
    resource_key: str
    resource_type: str
    action_slug: str
    effect: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class PrivilegeHandler:
    """Smoke handler projecting artifact_type='privilege' into NormalizationResult.

    Behaviour is identical to RoleHandler at Phase 12 level. Intentionally
    kept as a separate class to allow independent evolution in Phase 13+.
    Step 15: smoke-level coverage.
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
            payload = _PrivilegePayload.model_validate(artifact.payload)
        except ValidationError as exc:
            logger.debug(
                'privilege handler: invalid payload for artifact %s — %s',
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


register_handler('privilege', PrivilegeHandler())

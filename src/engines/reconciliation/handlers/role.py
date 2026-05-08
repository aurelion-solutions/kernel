# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Handler for artifact_type='role'.

Payload keys expected from Lens CSV upload:
  account_external_id  — account username (string); resolved to subject_id via Account lookup
  resource_key         — string
  resource_type        — string
  action_slug          — string
  effect               — string
  valid_from           — ISO datetime (optional)
  valid_until          — ISO datetime (optional)

Resolution:
  SELECT Account WHERE username=account_external_id AND application_id=artifact.application_id
  → account.subject_id  (None → skip, account has no linked subject)

Invalid payload or unresolvable account → [] + DEBUG log.
"""

from __future__ import annotations

from datetime import datetime
import logging

from pydantic import BaseModel, ValidationError
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.reconciliation.contracts import NormalizationResult
from src.engines.reconciliation.registry import register_handler
from src.inventory.access_artifacts.schemas import AccessArtifactView
from src.inventory.accounts.models import Account
from src.inventory.resources.service import ResourceService

logger = logging.getLogger('reconciliation.handlers.role')


class _RolePayload(BaseModel):
    """Expected shape of AccessArtifact.payload for artifact_type='role'.

    Only account_external_id is required. Everything else has sensible defaults
    for the common case where the role name itself is the resource:
      resource_key  → artifact_name (the role name)
      resource_type → "role"
      action_slug   → "use"
      effect        → falls back to artifact.effect, then "allow"
    """

    account_external_id: str
    artifact_name: str | None = None  # role name; used as resource_key fallback
    resource_key: str | None = None
    resource_type: str = 'role'
    action_slug: str = 'use'
    effect: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class RoleHandler:
    """Handler projecting artifact_type='role' into NormalizationResult.

    Resolves account_external_id → subject_id via Account lookup.
    """

    def __init__(self, resource_service: ResourceService | None = None) -> None:
        self._resource_service = resource_service if resource_service is not None else ResourceService()

    async def handle(
        self,
        artifact: AccessArtifactView,
        session: AsyncSession,
    ) -> list[NormalizationResult]:
        """Parse payload, resolve account → subject, resolve resource, return result or []."""
        try:
            payload = _RolePayload.model_validate(artifact.payload)
        except ValidationError as exc:
            logger.debug(
                'role handler: invalid payload for artifact %s — %s',
                artifact.id,
                exc,
            )
            return []

        # Resolve account_external_id → Account → subject_id
        stmt = sa.select(Account).where(
            Account.username == payload.account_external_id,
            Account.application_id == artifact.application_id,
        )
        result = await session.execute(stmt)
        account = result.scalar_one_or_none()

        if account is None:
            logger.debug(
                'role handler: account %r not found for application %s',
                payload.account_external_id,
                artifact.application_id,
            )
            return []

        resource_key = payload.resource_key or payload.artifact_name
        if not resource_key:
            logger.debug('role handler: no resource_key or artifact_name for artifact %s', artifact.id)
            return []

        effect = payload.effect or artifact.effect or 'allow'

        resource = await self._resource_service.ensure_resource_by_identity(
            session,
            application_id=artifact.application_id,
            resource_type=payload.resource_type,
            resource_key=resource_key,
        )

        return [
            NormalizationResult(
                subject_id=account.subject_id,  # None when owner unknown — valid
                account_id=account.id,
                resource_id=resource.id,
                action_slug=payload.action_slug,
                effect=effect,
                valid_from=payload.valid_from,
                valid_until=payload.valid_until,
            )
        ]


register_handler('role', RoleHandler())

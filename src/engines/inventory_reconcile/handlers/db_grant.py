# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke handler for artifact_type='db_grant'.

Reads payload keys: subject_id, resource_type, resource_key, privileges (list),
effect, optional valid_from / valid_until.

Privilege → action_slug mapping:
  SELECT       → read
  INSERT       → write
  UPDATE       → write
  DELETE       → write
  EXECUTE      → execute
  ADMIN OPTION → admin

Non-standard privileges (TRUNCATE, REFERENCES, TRIGGER, etc.) are silently
dropped. If all privileges are non-standard, returns [].
Duplicate slugs from multiple privileges are deduplicated (e.g.
INSERT + UPDATE + DELETE → one 'write' result).

Operator visibility: non-standard privileges are logged at INFO level so they
appear in production logs without requiring debug logging enabled.
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

logger = logging.getLogger('reconciliation.handlers.db_grant')

_PRIVILEGE_TO_ACTION_SLUG: dict[str, str] = {
    'SELECT': 'read',
    'INSERT': 'write',
    'UPDATE': 'write',
    'DELETE': 'write',
    'EXECUTE': 'execute',
    'ADMIN OPTION': 'admin',
}


class _DbGrantPayload(BaseModel):
    """Expected shape of AccessArtifact.payload for artifact_type='db_grant'."""

    subject_id: UUID
    resource_type: str
    resource_key: str
    privileges: list[str]
    effect: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class DbGrantHandler:
    """Smoke handler projecting artifact_type='db_grant' into NormalizationResult list.

    One NormalizationResult per distinct mapped action_slug. Non-standard
    privileges are silently dropped (logged at INFO). Step 15: smoke-level coverage.
    """

    def __init__(self, resource_service: ResourceService | None = None) -> None:
        self._resource_service = resource_service if resource_service is not None else ResourceService()

    async def handle(
        self,
        artifact: AccessArtifactView,
        session: AsyncSession,
    ) -> list[NormalizationResult]:
        """Parse payload, resolve resource, return results or []."""
        try:
            payload = _DbGrantPayload.model_validate(artifact.payload)
        except ValidationError as exc:
            logger.debug(
                'db_grant handler: invalid payload for artifact %s — %s',
                artifact.id,
                exc,
            )
            return []

        mapped_slugs: set[str] = set()
        skipped: list[str] = []

        for priv in payload.privileges:
            slug = _PRIVILEGE_TO_ACTION_SLUG.get(priv)
            if slug is None:
                skipped.append(priv)
            else:
                mapped_slugs.add(slug)

        if skipped:
            logger.info(
                'db_grant handler: artifact %s — non-standard privileges dropped (no vocabulary mapping): %s',
                artifact.id,
                ', '.join(skipped),
            )

        if not mapped_slugs:
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
                action_slug=slug,
                effect=payload.effect,
                valid_from=payload.valid_from,
                valid_until=payload.valid_until,
            )
            for slug in sorted(mapped_slugs)  # sorted for deterministic ordering
        ]


register_handler('db_grant', DbGrantHandler())

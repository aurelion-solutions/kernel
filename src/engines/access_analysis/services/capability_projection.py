# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityProjectionService — projects EffectiveGrants into CapabilityGrants.

Lives in engines/access_analysis because it depends on both:
  - engines/effective_access  (EffectiveGrant source)
  - inventory/access_model    (CapabilityGrant target)

inventory/access_model must not depend on any engine layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_effective.models import EffectiveGrant
from src.engines.access_effective.repository import get_effective_grant
from src.inventory.access_model.capability_grants import capability_projector
from src.inventory.access_model.capability_grants.capability_projector import (
    CapabilityGrantDraft,
    EffectiveGrantView,
)
from src.inventory.access_model.capability_grants.exceptions import (
    EffectiveGrantNotFoundForProjectionError,
)
from src.inventory.access_model.capability_grants.mapping_loader import load_active_mappings
from src.inventory.access_model.capability_grants.repository import (
    UpsertResult,
    tombstone_capability_grants_for_effective_grant,
    upsert_capability_grants,
)
from src.inventory.access_model.capability_grants.schemas import ProjectionRunSummary
from src.inventory.access_model.capability_mappings.service import resolve_default_scope_key_id
from src.inventory.resources.models import Resource

_BATCH_SIZE = 500


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


async def _load_resource(session: AsyncSession, resource_id: UUID) -> Resource:
    stmt = sa.select(Resource).where(Resource.id == resource_id)
    result = await session.execute(stmt)
    resource = result.scalars().one_or_none()
    if resource is None:
        raise ValueError(f'Resource {resource_id} not found — cannot build EffectiveGrantView')
    return resource


async def _load_resource_attributes(session: AsyncSession, resource_id: UUID) -> dict[str, str]:
    try:
        stmt = sa.text('SELECT key, value FROM resource_attributes WHERE resource_id = :rid')
        result = await session.execute(stmt, {'rid': str(resource_id)})
        return {row.key: row.value for row in result.all()}
    except Exception:  # noqa: BLE001 # allowed-broad: provider boundary
        return {}


async def _build_grant_view(
    session: AsyncSession,
    eg: EffectiveGrant,
) -> EffectiveGrantView:
    resource = await _load_resource(session, eg.resource_id)  # type: ignore[arg-type]
    resource_attributes = await _load_resource_attributes(session, eg.resource_id)  # type: ignore[arg-type]
    subject_attributes: dict[str, str] = {}

    return EffectiveGrantView(
        id=eg.id,  # type: ignore[arg-type]
        subject_id=eg.subject_id,  # type: ignore[arg-type]
        application_id=eg.application_id,  # type: ignore[arg-type]
        resource_id=eg.resource_id,  # type: ignore[arg-type]
        action_slug=eg.action.value,
        tombstoned_at=eg.tombstoned_at,
        resource_kind=resource.kind,
        resource_external_id=resource.external_id,
        resource_attributes=resource_attributes,
        subject_attributes=subject_attributes,
    )


def _accumulate(total: UpsertResult, batch: UpsertResult) -> UpsertResult:
    return UpsertResult(
        rows_upserted=total.rows_upserted + batch.rows_upserted,
        rows_inserted=total.rows_inserted + batch.rows_inserted,
        rows_updated=total.rows_updated + batch.rows_updated,
        rows_tombstoned=total.rows_tombstoned + batch.rows_tombstoned,
    )


# ---------------------------------------------------------------------------
# Projection service
# ---------------------------------------------------------------------------


class CapabilityProjectionService:
    """Projects EffectiveGrants into CapabilityGrants via active CapabilityMappings.

    No EventService, no LogService — event emission is ScanEngine's job (Step 14).
    Session discipline: flush inside, commit is the caller's responsibility.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def project_for_effective_grant(
        self,
        *,
        effective_grant_id: UUID,
        now: datetime,
    ) -> ProjectionRunSummary:
        """Project one EffectiveGrant against active mappings and upsert results.

        Raises EffectiveGrantNotFoundForProjectionError if the EG does not exist.
        """
        started_at = now

        eg = await get_effective_grant(self._session, effective_grant_id)
        if eg is None:
            raise EffectiveGrantNotFoundForProjectionError(effective_grant_id)

        grant_view = await _build_grant_view(self._session, eg)
        active_mappings = await load_active_mappings(self._session)
        global_scope_key_id = await resolve_default_scope_key_id(self._session)

        drafts: list[CapabilityGrantDraft] = capability_projector.project_grant(
            grant_view,
            active_mappings,
            now=now,
            global_scope_key_id=global_scope_key_id,
        )

        upsert_result = await upsert_capability_grants(self._session, drafts)
        await self._session.flush()

        finished_at = datetime.now(UTC)
        return ProjectionRunSummary(
            scope_kind='effective_grant',
            scope_id=effective_grant_id,
            pairs_projected=len(drafts),
            rows_upserted=upsert_result.rows_upserted,
            rows_inserted=upsert_result.rows_inserted,
            rows_updated=upsert_result.rows_updated,
            rows_tombstoned=upsert_result.rows_tombstoned,
            started_at=started_at,
            finished_at=finished_at,
        )

    async def project_for_application(
        self,
        *,
        application_id: UUID,
        now: datetime,
    ) -> ProjectionRunSummary:
        """Project all live EGs for an application. Keyset-paginated in batches of 500."""
        started_at = now

        active_mappings = await load_active_mappings(self._session)
        global_scope_key_id = await resolve_default_scope_key_id(self._session)

        total_pairs = 0
        total_result = UpsertResult(rows_upserted=0, rows_inserted=0, rows_updated=0, rows_tombstoned=0)

        last_id: UUID | None = None

        while True:
            base_stmt = sa.select(EffectiveGrant).where(EffectiveGrant.application_id == application_id)
            if last_id is None:
                batch_stmt = base_stmt.order_by(EffectiveGrant.id).limit(_BATCH_SIZE)
            else:
                batch_stmt = base_stmt.where(EffectiveGrant.id > last_id).order_by(EffectiveGrant.id).limit(_BATCH_SIZE)

            result = await self._session.execute(batch_stmt)
            eg_batch = list(result.scalars().all())
            if not eg_batch:
                break

            all_drafts: list[CapabilityGrantDraft] = []
            for eg in eg_batch:
                grant_view = await _build_grant_view(self._session, eg)
                drafts = capability_projector.project_grant(
                    grant_view,
                    active_mappings,
                    now=now,
                    global_scope_key_id=global_scope_key_id,
                )
                all_drafts.extend(drafts)
                total_pairs += len(drafts)

            if all_drafts:
                batch_result = await upsert_capability_grants(self._session, all_drafts)
                total_result = _accumulate(total_result, batch_result)

            last_id = eg_batch[-1].id
            if len(eg_batch) < _BATCH_SIZE:
                break

        await self._session.flush()

        finished_at = datetime.now(UTC)
        return ProjectionRunSummary(
            scope_kind='application',
            scope_id=application_id,
            pairs_projected=total_pairs,
            rows_upserted=total_result.rows_upserted,
            rows_inserted=total_result.rows_inserted,
            rows_updated=total_result.rows_updated,
            rows_tombstoned=total_result.rows_tombstoned,
            started_at=started_at,
            finished_at=finished_at,
        )

    async def tombstone_for_effective_grant(
        self,
        *,
        effective_grant_id: UUID,
        observed_at: datetime,
    ) -> int:
        """Soft-delete all capability grants sourced from an effective grant."""
        count = await tombstone_capability_grants_for_effective_grant(
            self._session,
            effective_grant_id=effective_grant_id,
            observed_at=observed_at,
        )
        await self._session.flush()
        return count

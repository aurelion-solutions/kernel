# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Display enrichment for access_facts list endpoint.

Facts are read from DuckDB lake; display names come from PostgreSQL.
Two round-trips total: one DuckDB scan (done upstream) + one PG batch
lookup (done here).  Never N+1.
"""

from __future__ import annotations

from uuid import UUID

# resources → application requires a separate resource→app_id lookup
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_facts.schemas import AccessFactRead, AccessFactView
from src.inventory.display_lookups import (
    batch_account_display,
    batch_application_display,
    batch_display_by_subject_ids,
    batch_resource_display,
)
from src.inventory.resources.models import Resource


async def _batch_resource_application(
    session: AsyncSession,
    resource_ids: set[UUID],
) -> dict[UUID, UUID]:
    """Return {resource_id: application_id} for the given resource UUIDs."""
    if not resource_ids:
        return {}
    stmt = sa.select(Resource.id, Resource.application_id).where(Resource.id.in_(resource_ids))
    result = await session.execute(stmt)
    return {row.id: row.application_id for row in result.all()}


async def enrich_access_facts(
    session: AsyncSession,
    views: list[AccessFactView],
) -> list[AccessFactRead]:
    """Enrich a page of AccessFactView with display names from PG.

    Steps:
    1. Collect unique subject_ids, account_ids, resource_ids.
    2. Try employee lookup for all subject_ids; remainder → NHI lookup.
    3. Batch account lookup.
    4. Batch resource lookup (display string).
    5. Resolve resource → application_id, then batch application lookup.
    6. Map each view → AccessFactRead with display fields populated.
    """
    subject_ids: set[UUID] = {v.subject_id for v in views}
    account_ids: set[UUID] = {v.account_id for v in views if v.account_id is not None}
    resource_ids: set[UUID] = {v.resource_id for v in views}

    subject_map = await batch_display_by_subject_ids(session, subject_ids)

    account_map = await batch_account_display(session, account_ids)
    resource_map = await batch_resource_display(session, resource_ids)

    # resource → application
    res_app_map = await _batch_resource_application(session, resource_ids)
    app_ids: set[UUID] = set(res_app_map.values())
    app_map = await batch_application_display(session, app_ids)

    result: list[AccessFactRead] = []
    for v in views:
        app_id = res_app_map.get(v.resource_id)
        app_display = app_map.get(app_id) if app_id else None
        result.append(
            AccessFactRead(
                id=v.id,
                subject_id=v.subject_id,
                account_id=v.account_id,
                resource_id=v.resource_id,
                action_slug=v.action_slug,
                effect=v.effect,
                is_active=v.is_active,
                revoked_at=v.revoked_at,
                observed_at=v.observed_at,
                valid_from=v.valid_from,
                valid_until=v.valid_until,
                created_at=v.created_at,
                subject_display=subject_map.get(v.subject_id),
                account_display=account_map.get(v.account_id) if v.account_id else None,
                resource_display=resource_map.get(v.resource_id),
                application_code=app_display.code if app_display else None,
                application_name=app_display.name if app_display else None,
            )
        )

    return result

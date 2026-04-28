# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessUsageFact repository for PostgreSQL access."""

from __future__ import annotations

from datetime import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_usage_facts.models import AccessUsageFact


async def create_access_usage_fact(
    session: AsyncSession,
    *,
    access_fact_id: uuid.UUID,
    last_seen: datetime,
    usage_count: int = 0,
    window_from: datetime,
    window_to: datetime | None = None,
) -> AccessUsageFact:
    """Create and persist an access usage fact."""
    fact = AccessUsageFact(
        access_fact_id=access_fact_id,
        last_seen=last_seen,
        usage_count=usage_count,
        window_from=window_from,
        window_to=window_to,
    )
    session.add(fact)
    await session.flush()
    await session.refresh(fact)
    return fact


async def get_access_usage_fact_by_id(
    session: AsyncSession,
    usage_fact_id: uuid.UUID,
) -> AccessUsageFact | None:
    """Load access usage fact by id."""
    result = await session.execute(select(AccessUsageFact).where(AccessUsageFact.id == usage_fact_id))
    return result.scalar_one_or_none()


async def list_access_usage_facts(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    access_fact_id: uuid.UUID | None = None,
    since: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AccessUsageFact]:
    """List access usage facts with optional filters, ordered by last_seen DESC."""
    query = select(AccessUsageFact).order_by(AccessUsageFact.last_seen.desc())

    if subject_id is not None or resource_id is not None:
        # JOIN against access_facts without ORM dependency (model deleted Phase 15 Step 16).
        # Build a subquery that returns matching access_fact_ids.
        inner_predicates = []
        inner_params: dict = {}
        if subject_id is not None:
            inner_predicates.append('af.subject_id = :subject_id_join')
            inner_params['subject_id_join'] = subject_id
        if resource_id is not None:
            inner_predicates.append('af.resource_id = :resource_id_join')
            inner_params['resource_id_join'] = resource_id
        where_clause = ' AND '.join(inner_predicates)
        subq_sql = sa.text(
            f'SELECT af.id FROM access_facts af WHERE {where_clause}'  # noqa: S608
        ).bindparams(**inner_params)
        query = query.where(AccessUsageFact.access_fact_id.in_(subq_sql))

    if access_fact_id is not None:
        query = query.where(AccessUsageFact.access_fact_id == access_fact_id)

    if since is not None:
        query = query.where(AccessUsageFact.last_seen >= since)

    query = query.limit(min(limit, 200)).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())

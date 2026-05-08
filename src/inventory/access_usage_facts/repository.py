# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessUsageFact repository for PostgreSQL access."""

from __future__ import annotations

from datetime import datetime
import uuid

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
    """List access usage facts with optional filters, ordered by last_seen DESC.

    Phase 15: ``access_facts`` was dropped from PG — facts now live in Iceberg
    ``normalized.access_facts``. The legacy JOIN-against-PG subquery used for
    ``subject_id`` / ``resource_id`` filters is no longer possible from this
    layer; those filters are accepted but ignored here. A future revision must
    push them down via a lake query (iceberg_scan) and intersect with the
    resulting ``access_fact_id`` set.
    """
    del subject_id, resource_id  # explicit no-op until lake-side resolution lands

    query = select(AccessUsageFact).order_by(AccessUsageFact.last_seen.desc())

    if access_fact_id is not None:
        query = query.where(AccessUsageFact.access_fact_id == access_fact_id)

    if since is not None:
        query = query.where(AccessUsageFact.last_seen >= since)

    query = query.limit(min(limit, 200)).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())

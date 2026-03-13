# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Initiative repository for PostgreSQL access."""

from __future__ import annotations

from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.initiatives.models import Initiative, InitiativeType


async def create_initiative(
    session: AsyncSession,
    *,
    access_fact_id: uuid.UUID,
    type_: InitiativeType,
    origin: str,
    valid_from: Any = None,
    valid_until: Any = None,
) -> Initiative:
    """Create and persist an initiative. Omits valid_from when None so server default applies."""
    kwargs: dict[str, Any] = {
        'access_fact_id': access_fact_id,
        'type': type_,
        'origin': origin,
    }
    if valid_from is not None:
        kwargs['valid_from'] = valid_from
    if valid_until is not None:
        kwargs['valid_until'] = valid_until
    initiative = Initiative(**kwargs)
    session.add(initiative)
    await session.flush()
    await session.refresh(initiative)
    return initiative


async def get_initiative_by_id(
    session: AsyncSession,
    initiative_id: uuid.UUID,
) -> Initiative | None:
    """Load initiative by id."""
    result = await session.execute(select(Initiative).where(Initiative.id == initiative_id))
    return result.scalar_one_or_none()


async def list_initiatives(
    session: AsyncSession,
    *,
    access_fact_id: uuid.UUID | None = None,
    type_: InitiativeType | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[Initiative]:
    """List initiatives with optional filters, ordered by created_at DESC."""
    query = select(Initiative).order_by(Initiative.created_at.desc())
    if access_fact_id is not None:
        query = query.where(Initiative.access_fact_id == access_fact_id)
    if type_ is not None:
        query = query.where(Initiative.type == type_)
    query = query.limit(min(limit, 200)).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_initiative(
    session: AsyncSession,
    initiative: Initiative,
    *,
    fields: dict[str, Any],
) -> Initiative:
    """Apply field updates to an initiative, flush and refresh."""
    for key, value in fields.items():
        setattr(initiative, key, value)
    await session.flush()
    await session.refresh(initiative)
    return initiative

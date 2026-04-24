# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Capability repository — plain async functions over AsyncSession.

No commits. Service flushes; caller commits (per ARCH_CONTEXT transaction-ownership rule).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.capabilities.models import Capability


async def insert_capability(
    session: AsyncSession,
    *,
    slug: str,
    name: str,
    description: str | None,
    is_active: bool,
    created_by: str | None,
) -> Capability:
    """Insert a new Capability row and flush. Does not commit."""
    capability = Capability(
        slug=slug,
        name=name,
        description=description,
        is_active=is_active,
        created_by=created_by,
    )
    session.add(capability)
    await session.flush()
    await session.refresh(capability)
    return capability


async def get_capability_by_id(
    session: AsyncSession,
    capability_id: int,
) -> Capability | None:
    """Return the Capability with the given id, or None."""
    stmt = select(Capability).where(Capability.id == capability_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_capability_by_slug(
    session: AsyncSession,
    slug: str,
) -> Capability | None:
    """Return the Capability with the given slug, or None."""
    stmt = select(Capability).where(Capability.slug == slug)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_capabilities(
    session: AsyncSession,
    *,
    is_active: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Capability]:
    """Return capabilities ordered by id ASC, optionally filtered by is_active."""
    stmt = select(Capability).order_by(Capability.id.asc())
    if is_active is not None:
        stmt = stmt.where(Capability.is_active == is_active)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_capability_fields(
    session: AsyncSession,
    capability: Capability,
    *,
    name: str | None = None,
    description: str | None = None,
    is_active: bool | None = None,
) -> Capability:
    """Update only non-None fields on the capability, flush, and return the refreshed entity.

    ``slug`` is intentionally absent from this function's signature — slugs are immutable.
    """
    if name is not None:
        capability.name = name
    if description is not None:
        capability.description = description
    if is_active is not None:
        capability.is_active = is_active
    await session.flush()
    await session.refresh(capability)
    return capability

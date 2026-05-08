# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityScopeKey repository — plain async functions over AsyncSession.

No commits. Service flushes; caller commits (per ARCH_CONTEXT transaction-ownership rule).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_model.capability_scope_keys.models import CapabilityScopeKey


async def insert_capability_scope_key(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    description: str | None,
    is_active: bool,
    created_by: str | None,
) -> CapabilityScopeKey:
    """Insert a new CapabilityScopeKey row and flush. Does not commit."""
    scope_key = CapabilityScopeKey(
        code=code,
        name=name,
        description=description,
        is_active=is_active,
        created_by=created_by,
    )
    session.add(scope_key)
    await session.flush()
    await session.refresh(scope_key)
    return scope_key


async def get_capability_scope_key_by_id(
    session: AsyncSession,
    scope_key_id: int,
) -> CapabilityScopeKey | None:
    """Return the CapabilityScopeKey with the given id, or None."""
    stmt = select(CapabilityScopeKey).where(CapabilityScopeKey.id == scope_key_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_capability_scope_key_by_code(
    session: AsyncSession,
    code: str,
) -> CapabilityScopeKey | None:
    """Return the CapabilityScopeKey with the given code, or None."""
    stmt = select(CapabilityScopeKey).where(CapabilityScopeKey.code == code)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_capability_scope_keys(
    session: AsyncSession,
    *,
    is_active: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[CapabilityScopeKey]:
    """Return capability scope keys ordered by id ASC, optionally filtered by is_active."""
    stmt = select(CapabilityScopeKey).order_by(CapabilityScopeKey.id.asc())
    if is_active is not None:
        stmt = stmt.where(CapabilityScopeKey.is_active == is_active)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_capability_scope_key_fields(
    session: AsyncSession,
    scope_key: CapabilityScopeKey,
    *,
    name: str | None = None,
    description: str | None = None,
    is_active: bool | None = None,
) -> CapabilityScopeKey:
    """Update only non-None fields on the scope key, flush, and return the refreshed entity.

    ``code`` is intentionally absent from this function's signature — codes are immutable.
    """
    if name is not None:
        scope_key.name = name
    if description is not None:
        scope_key.description = description
    if is_active is not None:
        scope_key.is_active = is_active
    await session.flush()
    await session.refresh(scope_key)
    return scope_key

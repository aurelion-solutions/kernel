# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""NHI repository for PostgreSQL access."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.nhi.models import NHI, NHIAttribute


async def create_nhi(
    session: AsyncSession,
    *,
    external_id: str,
    name: str,
    kind: str,
    description: str | None = None,
    is_locked: bool = False,
    owner_employee_id: uuid.UUID | None = None,
    application_id: uuid.UUID | None = None,
) -> NHI:
    """Create and persist an NHI."""
    nhi = NHI(
        external_id=external_id,
        name=name,
        kind=kind,
        description=description,
        is_locked=is_locked,
        owner_employee_id=owner_employee_id,
        application_id=application_id,
    )
    session.add(nhi)
    await session.flush()
    await session.refresh(nhi)
    return nhi


async def get_nhi_by_id(
    session: AsyncSession,
    nhi_id: uuid.UUID,
) -> NHI | None:
    """Load NHI by id."""
    result = await session.execute(select(NHI).where(NHI.id == nhi_id))
    return result.scalar_one_or_none()


async def get_nhi_by_external_id(
    session: AsyncSession,
    external_id: str,
) -> NHI | None:
    """Load first NHI with the given external_id (ordered by id)."""
    result = await session.execute(select(NHI).where(NHI.external_id == external_id).order_by(NHI.id).limit(1))
    return result.scalars().first()


async def list_nhi(session: AsyncSession) -> list[NHI]:
    """List all NHIs."""
    result = await session.execute(select(NHI).order_by(NHI.external_id))
    return list(result.scalars().all())


async def list_nhi_by_application_id(
    session: AsyncSession,
    application_id: uuid.UUID,
) -> list[NHI]:
    """Return all NHIs whose application_id matches the given application UUID."""
    result = await session.execute(select(NHI).where(NHI.application_id == application_id).order_by(NHI.id))
    return list(result.scalars().all())


async def list_nhi_attributes(
    session: AsyncSession,
    nhi_id: uuid.UUID,
) -> list[NHIAttribute]:
    """List attributes for an NHI."""
    result = await session.execute(select(NHIAttribute).where(NHIAttribute.nhi_id == nhi_id).order_by(NHIAttribute.key))
    return list(result.scalars().all())


async def create_nhi_attribute(
    session: AsyncSession,
    *,
    nhi_id: uuid.UUID,
    key: str,
    value: str,
) -> NHIAttribute:
    """Create and persist an NHI attribute."""
    attr = NHIAttribute(
        nhi_id=nhi_id,
        key=key,
        value=value,
    )
    session.add(attr)
    await session.flush()
    await session.refresh(attr)
    return attr


async def get_nhi_attribute_by_key(
    session: AsyncSession,
    nhi_id: uuid.UUID,
    key: str,
) -> NHIAttribute | None:
    """Load NHI attribute by nhi_id and key."""
    result = await session.execute(
        select(NHIAttribute).where(
            NHIAttribute.nhi_id == nhi_id,
            NHIAttribute.key == key,
        )
    )
    return result.scalar_one_or_none()


async def delete_nhi_attribute(
    session: AsyncSession,
    nhi_id: uuid.UUID,
    key: str,
) -> bool:
    """Delete NHI attribute by nhi_id and key. Returns True if deleted."""
    attr = await get_nhi_attribute_by_key(session, nhi_id, key)
    if attr is None:
        return False
    await session.delete(attr)
    return True

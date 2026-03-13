# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Provider repository."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.secrets.provider_config.models import Provider


async def create_provider(
    session: AsyncSession,
    name: str,
    type: str,
    config: dict,
) -> Provider:
    """Create a provider."""
    provider = Provider(name=name, type=type, config=config)
    session.add(provider)
    await session.flush()
    await session.refresh(provider)
    return provider


async def get_by_name(session: AsyncSession, name: str) -> Provider | None:
    """Get provider by name."""
    result = await session.execute(select(Provider).where(Provider.name == name))
    return result.scalar_one_or_none()


async def get_by_id(session: AsyncSession, provider_id: uuid.UUID) -> Provider | None:
    """Get provider by id."""
    return await session.get(Provider, provider_id)


async def list_providers(session: AsyncSession) -> list[Provider]:
    """List all providers."""
    result = await session.execute(select(Provider).order_by(Provider.name))
    return list(result.scalars().all())


async def delete_provider(session: AsyncSession, name: str) -> bool:
    """Delete provider by name. Returns True if deleted."""
    provider = await get_by_name(session, name)
    if provider is None:
        return False
    await session.delete(provider)
    return True

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Secret repository."""

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.secrets.models import Secret


async def create_secret_metadata(
    session: AsyncSession,
    *,
    key: str,
    provider: str,
    namespace: str,
) -> Secret:
    """Create or overwrite secret metadata record."""
    stmt = insert(Secret).values(key=key, provider=provider, namespace=namespace)
    stmt = stmt.on_conflict_do_update(
        index_elements=['key', 'provider', 'namespace'],
        set_={'updated_at': func.now()},
    )
    await session.execute(stmt)
    result = await session.execute(
        select(Secret).where(
            Secret.key == key,
            Secret.provider == provider,
            Secret.namespace == namespace,
        )
    )
    return result.scalar_one()


async def delete_secret_metadata(
    session: AsyncSession,
    *,
    key: str,
    provider: str,
    namespace: str,
) -> None:
    """Delete secret metadata record."""
    await session.execute(
        delete(Secret).where(
            Secret.key == key,
            Secret.provider == provider,
            Secret.namespace == namespace,
        ),
    )


async def list_secrets(
    session: AsyncSession,
    *,
    provider: str | None = None,
    namespace: str | None = None,
) -> list[Secret]:
    """List secrets by optional provider and namespace filters."""
    stmt = select(Secret).order_by(Secret.provider, Secret.namespace, Secret.key)
    if provider:
        stmt = stmt.where(Secret.provider == provider)
    if namespace:
        stmt = stmt.where(Secret.namespace == namespace)
    result = await session.execute(stmt)
    return list(result.scalars().all())

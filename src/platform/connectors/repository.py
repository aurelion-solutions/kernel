# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.connectors.models import ConnectorInstance


async def upsert_connector_instance(
    session: AsyncSession,
    *,
    instance_id: str,
    tags: list[str],
) -> ConnectorInstance:
    stmt = insert(ConnectorInstance).values(
        instance_id=instance_id,
        tags=tags,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=['instance_id'],
        set_={
            'tags': tags,
            'last_seen_at': func.now(),
            'updated_at': func.now(),
        },
    )
    await session.execute(stmt)
    result = await session.execute(select(ConnectorInstance).where(ConnectorInstance.instance_id == instance_id))
    return result.scalar_one()


async def delete_stale_connector_instances(
    session: AsyncSession,
    *,
    offline_for: timedelta = timedelta(days=1),
) -> int:
    cutoff = datetime.now(UTC) - offline_for
    stmt = (
        delete(ConnectorInstance)
        .where(ConnectorInstance.last_seen_at < cutoff)
        .returning(ConnectorInstance.instance_id)
    )
    result = await session.execute(stmt)
    return len(list(result.fetchall()))


async def get_connector_instance_by_instance_id(
    session: AsyncSession,
    instance_id: str,
) -> ConnectorInstance | None:
    result = await session.execute(select(ConnectorInstance).where(ConnectorInstance.instance_id == instance_id))
    return result.scalar_one_or_none()


async def list_connector_instances(session: AsyncSession) -> list[ConnectorInstance]:
    result = await session.execute(select(ConnectorInstance).order_by(ConnectorInstance.instance_id))
    return list(result.scalars().all())


async def list_online_connector_instances(
    session: AsyncSession,
) -> list[ConnectorInstance]:
    result = await session.execute(select(ConnectorInstance).order_by(ConnectorInstance.instance_id))
    instances = list(result.scalars().all())
    return [instance for instance in instances if instance.is_online]

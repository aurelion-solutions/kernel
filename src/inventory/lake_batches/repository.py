# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake batch repository for PostgreSQL access."""

from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.lake_batches.models import LakeBatch


async def create_lake_batch(
    session: AsyncSession,
    *,
    storage_provider: str,
    dataset_type: str,
    storage_key: str,
    row_count: int,
    application_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    content_type: str | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> LakeBatch:
    """Create and persist a lake batch metadata row."""
    batch = LakeBatch(
        storage_provider=storage_provider,
        dataset_type=dataset_type,
        storage_key=storage_key,
        row_count=row_count,
        application_id=application_id,
        task_id=task_id,
        content_type=content_type,
        metadata_json=metadata_json,
    )
    session.add(batch)
    await session.flush()
    await session.refresh(batch)
    return batch


async def get_by_id(
    session: AsyncSession,
    batch_id: uuid.UUID,
) -> LakeBatch | None:
    """Load lake batch by id."""
    result = await session.execute(select(LakeBatch).where(LakeBatch.id == batch_id))
    return result.scalar_one_or_none()


async def delete_by_id(
    session: AsyncSession,
    batch_id: uuid.UUID,
) -> None:
    """Delete lake batch by id."""
    batch = await get_by_id(session, batch_id)
    if batch is not None:
        await session.delete(batch)

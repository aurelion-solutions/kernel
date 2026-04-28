# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake batch repository for PostgreSQL access."""

from datetime import datetime
from typing import Any
import uuid

from sqlalchemy import select, tuple_
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


async def create_iceberg_lake_batch(
    session: AsyncSession,
    *,
    dataset_type: str,
    iceberg_namespace: str,
    iceberg_table: str,
    snapshot_id: int,
    row_count: int,
    application_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    metadata_json: dict[str, Any] | None = None,
) -> LakeBatch:
    """Create and persist a lake batch metadata row for an Iceberg-origin write."""
    batch = LakeBatch(
        storage_provider=None,
        storage_key=None,
        content_type=None,
        dataset_type=dataset_type,
        iceberg_namespace=iceberg_namespace,
        iceberg_table=iceberg_table,
        snapshot_id=snapshot_id,
        row_count=row_count,
        application_id=application_id,
        task_id=task_id,
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


async def list_recent_batches(
    session: AsyncSession,
    *,
    limit: int,
    before_created_at: datetime | None = None,
    before_id: uuid.UUID | None = None,
) -> list[LakeBatch]:
    """List lake batches ordered by (created_at DESC, id DESC).

    Returns up to ``limit + 1`` rows so callers can detect next-page existence.
    When ``before_created_at`` and ``before_id`` are both provided, a row-value
    keyset predicate ``(created_at, id) < (before_created_at, before_id)`` is
    applied.
    """
    stmt = select(LakeBatch).order_by(LakeBatch.created_at.desc(), LakeBatch.id.desc()).limit(limit + 1)
    if before_created_at is not None and before_id is not None:
        stmt = stmt.where(tuple_(LakeBatch.created_at, LakeBatch.id) < tuple_(before_created_at, before_id))
    result = await session.execute(stmt)
    return list(result.scalars().all())

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for lake_migration repository cursor pagination."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.lake_migration.models import LakeMigrationDataset, LakeMigrationRun, LakeMigrationStatus
from src.capabilities.lake_migration.repository import list_runs


async def _create_run(session: AsyncSession, i: int) -> LakeMigrationRun:
    """Insert a lake_batch and a migration run for test seeding."""
    from src.inventory.lake_batches.models import LakeBatch  # noqa: PLC0415

    lb = LakeBatch(
        storage_provider=None,
        dataset_type='pg_migration.access_artifacts',
        storage_key=None,
        row_count=0,
    )
    session.add(lb)
    await session.flush()

    run = LakeMigrationRun(
        dataset=LakeMigrationDataset.access_artifacts,
        status=LakeMigrationStatus.completed if i % 3 == 0 else LakeMigrationStatus.failed,
        lake_batch_id=lb.id,
        rows_read=i,
        rows_written=i,
    )
    session.add(run)
    await session.flush()
    return run


@pytest.mark.asyncio
async def test_cursor_pagination(db_session: AsyncSession, session_factory) -> None:
    """50 runs with page_size=10 yields 5 pages, stable and ordered."""
    # Seed 50 runs
    for i in range(50):
        await _create_run(db_session, i)
    await db_session.commit()

    collected: list[uuid.UUID] = []
    cursor: str | None = None
    pages = 0

    async with session_factory() as s:
        while True:
            runs, next_cursor = await list_runs(s, limit=10, cursor=cursor)
            assert len(runs) <= 10
            for r in runs:
                assert r.id not in collected
                collected.append(r.id)
            pages += 1
            if next_cursor is None:
                break
            cursor = next_cursor

    assert len(collected) == 50
    assert pages == 5


@pytest.mark.asyncio
async def test_status_filter(db_session: AsyncSession, session_factory) -> None:
    """status_filter=failed returns only failed runs."""
    for i in range(6):
        await _create_run(db_session, i)
    await db_session.commit()

    async with session_factory() as s:
        runs, _ = await list_runs(s, status_filter=LakeMigrationStatus.failed, limit=100)
        assert all(r.status == LakeMigrationStatus.failed for r in runs)
        assert len(runs) > 0

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LakeBatchService.list_batches and cursor helpers."""

import base64
from datetime import UTC, datetime, timedelta
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.lake_batches.models import LakeBatch
from src.inventory.lake_batches.service import LakeBatchService, _decode_cursor, _encode_cursor
from src.platform.storage.factory import DataLakeStorageFactory


@pytest.fixture
def service() -> LakeBatchService:
    return LakeBatchService(storage_factory=DataLakeStorageFactory())


async def _insert_batch(session: AsyncSession, created_at: datetime) -> LakeBatch:
    """Insert a minimal lake batch with a specific created_at."""
    batch = LakeBatch(
        id=uuid.uuid4(),
        dataset_type='accounts',
        row_count=1,
    )
    session.add(batch)
    await session.flush()
    # Override server-default created_at via UPDATE to ensure deterministic ordering.
    await session.execute(
        __import__('sqlalchemy', fromlist=['text']).text('UPDATE lake_batches SET created_at = :ts WHERE id = :id'),
        {'ts': created_at, 'id': batch.id},
    )
    await session.refresh(batch)
    return batch


# ---------------------------------------------------------------------------
# test_service_list_returns_empty_when_no_rows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_list_returns_empty_when_no_rows(
    service: LakeBatchService,
    session_factory,
) -> None:
    async with session_factory() as session:
        batches, next_cursor = await service.list_batches(session, limit=10)
    assert batches == []
    assert next_cursor is None


# ---------------------------------------------------------------------------
# test_service_list_pagination_round_trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_list_pagination_round_trip(
    service: LakeBatchService,
    session_factory,
) -> None:
    base_ts = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    async with session_factory() as session:
        # Insert 5 batches with distinct timestamps (newest first means largest ts)
        inserted = []
        for i in range(5):
            b = await _insert_batch(session, base_ts + timedelta(seconds=i))
            inserted.append(b)
        await session.commit()

    # Expected order: newest first (i=4, 3, 2, 1, 0)
    expected_ids = [b.id for b in sorted(inserted, key=lambda x: x.created_at, reverse=True)]

    collected: list[uuid.UUID] = []
    cursor: str | None = None
    page_count = 0

    async with session_factory() as session:
        while True:
            batches, cursor = await service.list_batches(session, limit=2, cursor=cursor)
            assert len(batches) <= 2
            collected.extend(b.id for b in batches)
            page_count += 1
            if cursor is None:
                break

    assert page_count == 3  # 2 + 2 + 1
    assert len(collected) == 5
    # No duplicates
    assert len(set(collected)) == 5
    # Full order matches expected newest-first order
    assert collected == expected_ids


# ---------------------------------------------------------------------------
# test_service_list_orders_by_created_at_desc_then_id_desc
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_list_orders_by_created_at_desc_then_id_desc(
    service: LakeBatchService,
    session_factory,
) -> None:
    """Two batches with identical created_at: sorted by id DESC."""
    same_ts = datetime(2026, 6, 1, 0, 0, 0, tzinfo=UTC)
    async with session_factory() as session:
        b1 = await _insert_batch(session, same_ts)
        b2 = await _insert_batch(session, same_ts)
        await session.commit()

    # The one with the larger UUID hex comes first (DESC)
    expected_first = max(b1.id, b2.id)

    async with session_factory() as session:
        batches, _ = await service.list_batches(session, limit=10)
    # Filter to only our two batches (DB may have rows from other tests)
    our_ids = {b1.id, b2.id}
    our_batches = [b for b in batches if b.id in our_ids]
    assert len(our_batches) == 2
    assert our_batches[0].id == expected_first


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def test_service_list_cursor_round_trip() -> None:
    dt = datetime(2026, 3, 15, 10, 30, 0, tzinfo=UTC)
    bid = uuid.UUID('12345678-1234-5678-1234-567812345678')
    encoded = _encode_cursor(dt, bid)
    decoded_dt, decoded_id = _decode_cursor(encoded)
    assert decoded_dt == dt
    assert decoded_id == bid


def test_service_list_cursor_decode_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        _decode_cursor('not-base64!!!')


def test_service_list_cursor_decode_rejects_wrong_shape() -> None:
    # Valid base64 but no '|' separator
    encoded = base64.urlsafe_b64encode(b'hello').decode()
    with pytest.raises(ValueError):
        _decode_cursor(encoded)

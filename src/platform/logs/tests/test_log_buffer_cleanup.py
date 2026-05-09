# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for log_event_buffer retention cleanup."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from src.platform.logs.buffer_cleanup import (
    cutoff_before_retention,
    run_log_buffer_cleanup_once,
)
from src.platform.logs.buffer_repository import insert_buffered_log_event
from src.platform.logs.models import LogEventBufferRow
from src.platform.logs.schemas import LogEvent, LogLevel, LogParticipantKind, new_root_log_event


def _event(*, ts: datetime, suffix: str = '') -> LogEvent:
    return new_root_log_event(
        level=LogLevel.INFO,
        message='m',
        component='c',
        initiator_type=LogParticipantKind.SYSTEM,
        initiator_id='i',
        actor_type=LogParticipantKind.SYSTEM,
        actor_id='a',
        target_type=LogParticipantKind.SYSTEM,
        target_id='t',
        timestamp=ts,
    )


def test_cutoff_before_retention_uses_ttl_seconds() -> None:
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
    cutoff = cutoff_before_retention(retention_seconds=90, now=now)
    assert cutoff == now - timedelta(seconds=90)


def test_runtime_settings_default_log_buffer_retention_is_one_hour() -> None:
    from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

    config = RuntimeSettingsConfig()
    assert config.log_buffer_retention_seconds == 3600


@pytest.mark.asyncio
async def test_cleanup_deletes_expired_preserves_fresh(session_factory) -> None:
    """Rows with event timestamp before (now - TTL) are removed; newer rows stay."""
    now = datetime(2026, 6, 15, 18, 0, 0, tzinfo=UTC)
    old = _event(ts=now - timedelta(hours=3), suffix='-old')
    fresh = _event(ts=now - timedelta(minutes=20), suffix='-new')

    async with session_factory() as session:
        await insert_buffered_log_event(session, old)
        await insert_buffered_log_event(session, fresh)
        await session.commit()

    deleted = await run_log_buffer_cleanup_once(
        session_factory,
        retention_seconds=3600,
        now=now,
    )
    assert deleted == 1

    async with session_factory() as session:
        result = await session.execute(select(LogEventBufferRow))
        rows = list(result.scalars().all())

    assert len(rows) == 1
    assert rows[0].event_id == fresh.event_id


@pytest.mark.asyncio
async def test_cleanup_respects_longer_retention_config(session_factory) -> None:
    """Larger retention_seconds keeps borderline-old rows that short TTL would drop."""
    now = datetime(2026, 6, 15, 18, 0, 0, tzinfo=UTC)
    borderline = _event(ts=now - timedelta(hours=2), suffix='-mid')

    async with session_factory() as session:
        await insert_buffered_log_event(session, borderline)
        await session.commit()

    short_deleted = await run_log_buffer_cleanup_once(
        session_factory,
        retention_seconds=3600,
        now=now,
    )
    assert short_deleted == 1

    async with session_factory() as session:
        await insert_buffered_log_event(session, borderline)
        await session.commit()

    long_deleted = await run_log_buffer_cleanup_once(
        session_factory,
        retention_seconds=10_000_000,
        now=now,
    )
    assert long_deleted == 0

    async with session_factory() as session:
        result = await session.execute(select(LogEventBufferRow))
        assert len(list(result.scalars().all())) == 1


@pytest.mark.asyncio
async def test_cleanup_deletes_nothing_when_all_fresh(session_factory) -> None:
    now = datetime(2026, 6, 15, 18, 0, 0, tzinfo=UTC)
    e = _event(ts=now - timedelta(seconds=30), suffix='-fresh')

    async with session_factory() as session:
        await insert_buffered_log_event(session, e)
        await session.commit()

    deleted = await run_log_buffer_cleanup_once(
        session_factory,
        retention_seconds=3600,
        now=now,
    )
    assert deleted == 0

    async with session_factory() as session:
        result = await session.execute(select(LogEventBufferRow))
        assert len(list(result.scalars().all())) == 1

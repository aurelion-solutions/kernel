# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""One-shot retention cleanup for ``log_event_buffer`` (internal debug buffer)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.logs.buffer_repository import delete_log_buffer_rows_older_than_event_time


def cutoff_before_retention(*, retention_seconds: int, now: datetime | None = None) -> datetime:
    """``now`` minus ``retention_seconds`` in UTC (for tests, pass a fixed ``now``)."""
    t = now if now is not None else datetime.now(UTC)
    if t.tzinfo is None:
        t = t.replace(tzinfo=UTC)
    return t - timedelta(seconds=retention_seconds)


async def run_log_buffer_cleanup_once(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    retention_seconds: int,
    now: datetime | None = None,
) -> int:
    """Delete expired rows; commit; return deleted count."""
    cutoff = cutoff_before_retention(retention_seconds=retention_seconds, now=now)
    async with session_factory() as session:
        deleted = await delete_log_buffer_rows_older_than_event_time(session, cutoff=cutoff)
        await session.commit()
    return deleted

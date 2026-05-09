# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Persist normalized log events into the short-term PostgreSQL buffer."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.logs.models import LogEventBufferRow
from src.platform.logs.schemas import LogEvent


def log_event_to_buffer_row(event: LogEvent) -> LogEventBufferRow:
    """Map a validated :class:`LogEvent` to a buffer ORM row (no I/O)."""
    return LogEventBufferRow(
        event_id=event.event_id,
        timestamp=event.timestamp,
        level=event.level.value,
        message=event.message,
        component=event.component,
        correlation_id=event.correlation_id,
        causation_id=event.causation_id,
        payload=dict(event.payload),
        initiator_type=event.initiator_type.value,
        initiator_id=event.initiator_id,
        actor_type=event.actor_type.value,
        actor_id=event.actor_id,
        target_type=event.target_type.value,
        target_id=event.target_id,
    )


async def insert_buffered_log_event(session: AsyncSession, event: LogEvent) -> None:
    """Insert one buffer row. Caller commits."""
    session.add(log_event_to_buffer_row(event))
    await session.flush()


async def query_buffered_log_events(
    session: AsyncSession,
    *,
    correlation_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    initiator_type: str | None = None,
    initiator_id: str | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
    level: str | None = None,
    from_ts: datetime | None = None,
    to_ts: datetime | None = None,
    order_desc: bool = True,
    limit: int,
) -> Sequence[LogEventBufferRow]:
    """Select buffer rows matching optional filters, ordered by event ``timestamp``."""
    stmt = select(LogEventBufferRow)
    conditions: list = []
    if correlation_id is not None:
        conditions.append(LogEventBufferRow.correlation_id == correlation_id)
    if target_type is not None:
        conditions.append(LogEventBufferRow.target_type == target_type)
    if target_id is not None:
        conditions.append(LogEventBufferRow.target_id == target_id)
    if initiator_type is not None:
        conditions.append(LogEventBufferRow.initiator_type == initiator_type)
    if initiator_id is not None:
        conditions.append(LogEventBufferRow.initiator_id == initiator_id)
    if actor_type is not None:
        conditions.append(LogEventBufferRow.actor_type == actor_type)
    if actor_id is not None:
        conditions.append(LogEventBufferRow.actor_id == actor_id)
    if level is not None:
        conditions.append(LogEventBufferRow.level == level)
    if from_ts is not None:
        conditions.append(LogEventBufferRow.timestamp >= from_ts)
    if to_ts is not None:
        conditions.append(LogEventBufferRow.timestamp <= to_ts)
    if conditions:
        stmt = stmt.where(and_(*conditions))
    order_col = LogEventBufferRow.timestamp.desc() if order_desc else LogEventBufferRow.timestamp.asc()
    stmt = stmt.order_by(order_col).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def delete_log_buffer_rows_older_than_event_time(
    session: AsyncSession,
    *,
    cutoff: datetime,
) -> int:
    """Delete buffer rows whose event ``timestamp`` is strictly before ``cutoff`` (timezone-aware).

    Caller commits. Returns the number of rows deleted (``CursorResult.rowcount``).
    """
    result = await session.execute(
        delete(LogEventBufferRow).where(LogEventBufferRow.timestamp < cutoff),
    )
    return int(result.rowcount or 0)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Consume MQ log buffer queue messages and persist normalized :class:`LogEvent` rows."""

import asyncio
import json
import threading
from typing import Any, Literal

from pika.adapters.blocking_connection import BlockingChannel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from src.platform.logs.buffer_repository import insert_buffered_log_event
from src.platform.logs.consumer import parse_connector_log_payload

Outcome = Literal['persisted', 'bad_message', 'commit_failed']

# pika runs sync callbacks; we must not call ``asyncio.run`` per message: each run creates
# and destroys an event loop while SQLAlchemy's asyncpg pool keeps connections bound to the
# previous loop, which then raises "Event loop is closed" during pool teardown.
_BG_LOOP_LOCK = threading.Lock()
_bg_loop: asyncio.AbstractEventLoop | None = None


def _persist_worker_loop() -> asyncio.AbstractEventLoop:
    global _bg_loop
    with _BG_LOOP_LOCK:
        if _bg_loop is not None and _bg_loop.is_running():
            return _bg_loop
        _bg_loop = None

        ready = threading.Event()
        holder: list[asyncio.AbstractEventLoop] = []

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            holder.append(loop)
            ready.set()
            loop.run_forever()

        thread = threading.Thread(
            target=_run,
            name='aurelion-log-buffer-db',
            daemon=True,
        )
        thread.start()
        ready.wait()
        _bg_loop = holder[0]
        return _bg_loop


async def persist_buffer_message_body(session: AsyncSession, body: bytes) -> Outcome:
    """Parse body as JSON, validate as :class:`LogEvent`, insert buffer row (flush only)."""
    try:
        raw = json.loads(body.decode('utf-8'))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return 'bad_message'

    if not isinstance(raw, dict):
        return 'bad_message'

    event = parse_connector_log_payload(raw)
    if event is None:
        return 'bad_message'

    await insert_buffered_log_event(session, event)
    return 'persisted'


async def commit_buffer_delivery_async(
    session_factory: async_sessionmaker[AsyncSession],
    body: bytes,
) -> Outcome:
    """Parse, insert, commit or rollback. Use from async tests or wrapped with ``asyncio.run``."""
    async with session_factory() as session:
        try:
            outcome = await persist_buffer_message_body(session, body)
            if outcome == 'persisted':
                await session.commit()
            else:
                await session.rollback()
            return outcome
        except Exception:
            await session.rollback()
            return 'commit_failed'


def run_persist_buffer_message_blocking(
    session_factory: async_sessionmaker[AsyncSession],
    body: bytes,
) -> Outcome:
    """Run async persist on a dedicated long-lived loop (sync / pika thread safe)."""
    loop = _persist_worker_loop()
    fut = asyncio.run_coroutine_threadsafe(
        commit_buffer_delivery_async(session_factory, body),
        loop,
    )
    return fut.result(timeout=120)


def apply_buffer_outcome_to_channel(
    ch: BlockingChannel,
    method: Any,
    outcome: Outcome,
) -> None:
    """Map persistence outcome to RabbitMQ ack/nack (sync)."""
    tag = method.delivery_tag
    if outcome == 'persisted':
        ch.basic_ack(delivery_tag=tag)
    elif outcome == 'bad_message':
        ch.basic_nack(delivery_tag=tag, requeue=False)
    else:
        ch.basic_nack(delivery_tag=tag, requeue=True)


def buffer_queue_callback(
    ch: BlockingChannel,
    method: Any,
    _props: Any,
    body: bytes,
    *,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """pika callback: ack only after successful commit; drop poison; requeue on DB errors."""
    outcome = run_persist_buffer_message_blocking(session_factory, body)
    apply_buffer_outcome_to_channel(ch, method, outcome)

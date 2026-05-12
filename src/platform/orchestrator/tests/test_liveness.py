# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the heartbeat_publisher coroutine in liveness.py."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import MagicMock

from src.platform.events.schemas import EventEnvelope
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService
from src.platform.orchestrator.liveness import _EVENT_TYPE, heartbeat_publisher
from src.platform.orchestrator.liveness_schemas import ExecutorHeartbeatPayload
from src.platform.orchestrator.runner import WorkerIdentity

_STARTED_AT = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeEventService:
    """Records emitted EventEnvelopes; optionally raises on certain calls."""

    def __init__(self, *, fail_every: int | None = None) -> None:
        self.emitted: list[EventEnvelope] = []
        self._fail_every = fail_every
        self._call_count = 0

    async def emit(self, event: EventEnvelope) -> None:
        self._call_count += 1
        if self._fail_every is not None and self._call_count % self._fail_every == 0:
            raise RuntimeError('simulated sink failure')
        self.emitted.append(event)


def _make_log_spy() -> tuple[MagicMock, list[tuple[LogLevel, str]]]:
    mock_log = MagicMock(spec=LogService)
    calls: list[tuple[LogLevel, str]] = []

    def _emit_safe(level: LogLevel, message: str, *, component: str, payload: dict, **kw: object) -> None:
        calls.append((level, message))

    mock_log.emit_safe.side_effect = _emit_safe
    return mock_log, calls


def _make_worker() -> WorkerIdentity:
    return WorkerIdentity(worker_id='testhost-999-0', hostname='testhost', pid=999, slot_index=0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHeartbeatPublisherSingleEmit:
    async def test_emits_once_on_entry_then_stops(self) -> None:
        """Sets stop_event immediately → exactly 1 emission, clean shutdown log."""
        fake_events = _FakeEventService()
        mock_log, log_calls = _make_log_spy()
        worker = _make_worker()
        stop_event = asyncio.Event()
        stop_event.set()  # stop immediately after first tick

        await heartbeat_publisher(
            events=fake_events,  # type: ignore[arg-type]
            logs=mock_log,
            worker=worker,
            started_at=_STARTED_AT,
            pipelines_loaded=3,
            interval=0.05,
            stop_event=stop_event,
        )

        assert len(fake_events.emitted) == 1
        info_messages = [msg for lvl, msg in log_calls if lvl == LogLevel.INFO]
        assert any('stopped' in m for m in info_messages)

    async def test_emitted_envelope_shape(self) -> None:
        """Emitted envelope has correct event_type and payload."""
        fake_events = _FakeEventService()
        mock_log, _ = _make_log_spy()
        worker = _make_worker()
        stop_event = asyncio.Event()
        stop_event.set()

        await heartbeat_publisher(
            events=fake_events,  # type: ignore[arg-type]
            logs=mock_log,
            worker=worker,
            started_at=_STARTED_AT,
            pipelines_loaded=7,
            interval=0.05,
            stop_event=stop_event,
        )

        envelope = fake_events.emitted[0]
        assert envelope.event_type == _EVENT_TYPE
        payload = ExecutorHeartbeatPayload.model_validate({**envelope.payload, 'started_at': _STARTED_AT})
        assert payload.worker_id == worker.worker_id
        assert payload.slot_index == worker.slot_index
        assert payload.pipelines_loaded == 7


class TestHeartbeatPublisherMultipleTicks:
    async def test_emits_multiple_times_over_interval(self) -> None:
        """With interval=0.05s, run for ~0.18s → at least 2 emissions."""
        fake_events = _FakeEventService()
        mock_log, _ = _make_log_spy()
        worker = _make_worker()
        stop_event = asyncio.Event()

        async def _stopper() -> None:
            await asyncio.sleep(0.18)
            stop_event.set()

        stopper_task = asyncio.create_task(_stopper())
        await heartbeat_publisher(
            events=fake_events,  # type: ignore[arg-type]
            logs=mock_log,
            worker=worker,
            started_at=_STARTED_AT,
            pipelines_loaded=0,
            interval=0.05,
            stop_event=stop_event,
        )
        await stopper_task

        assert len(fake_events.emitted) >= 2


class TestHeartbeatPublisherFaultTolerance:
    async def test_stays_alive_on_emit_failure(self) -> None:
        """Fake EventService raises every other call → publisher stays alive, WARNING logged."""
        # fail_every=2 means calls 2, 4, ... raise
        fake_events = _FakeEventService(fail_every=2)
        mock_log, log_calls = _make_log_spy()
        worker = _make_worker()
        stop_event = asyncio.Event()

        async def _stopper() -> None:
            await asyncio.sleep(0.22)
            stop_event.set()

        stopper_task = asyncio.create_task(_stopper())
        await heartbeat_publisher(
            events=fake_events,  # type: ignore[arg-type]
            logs=mock_log,
            worker=worker,
            started_at=_STARTED_AT,
            pipelines_loaded=0,
            interval=0.05,
            stop_event=stop_event,
        )
        await stopper_task

        warnings = [msg for lvl, msg in log_calls if lvl == LogLevel.WARNING]
        assert len(warnings) >= 1
        assert all('heartbeat publish failed' in w for w in warnings)
        # Despite failures, successful emissions also happened
        assert len(fake_events.emitted) >= 1


class TestHeartbeatPublisherShutdown:
    async def test_exits_within_two_intervals(self) -> None:
        """stop_event.set() causes exit within 2 * interval seconds."""
        fake_events = _FakeEventService()
        mock_log, _ = _make_log_spy()
        worker = _make_worker()
        stop_event = asyncio.Event()
        interval = 0.05

        import time

        start = time.monotonic()

        async def _stopper() -> None:
            await asyncio.sleep(interval * 0.5)
            stop_event.set()

        stopper_task = asyncio.create_task(_stopper())
        await heartbeat_publisher(
            events=fake_events,  # type: ignore[arg-type]
            logs=mock_log,
            worker=worker,
            started_at=_STARTED_AT,
            pipelines_loaded=0,
            interval=interval,
            stop_event=stop_event,
        )
        await stopper_task
        elapsed = time.monotonic() - start
        assert elapsed < 2 * interval + 0.1  # generous buffer for CI

    async def test_no_leaked_tasks(self) -> None:
        """No orphan tasks remain after publisher exits."""
        fake_events = _FakeEventService()
        mock_log, _ = _make_log_spy()
        worker = _make_worker()
        stop_event = asyncio.Event()

        tasks_before = set(asyncio.all_tasks())
        stop_event.set()
        await heartbeat_publisher(
            events=fake_events,  # type: ignore[arg-type]
            logs=mock_log,
            worker=worker,
            started_at=_STARTED_AT,
            pipelines_loaded=0,
            interval=0.05,
            stop_event=stop_event,
        )
        # Give the event loop a tick to clean up
        await asyncio.sleep(0)
        tasks_after = set(asyncio.all_tasks()) - tasks_before
        # Filter out the test task itself
        leaked = {t for t in tasks_after if 'heartbeat_publisher' in repr(t)}
        assert not leaked

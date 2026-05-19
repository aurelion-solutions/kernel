# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for StepScopedLogService — the runner's per-step log façade.

The synchronous ``emit_safe`` path falls back to ``asyncio.run`` when called
without a running loop (see ``_schedule_or_run`` in logs/service.py), so the
captured sink receives the event before ``emit_safe`` returns. The async
``emit_log`` path is exercised through pytest-asyncio's auto mode.
"""

import uuid

import pytest
from src.platform.logs.interface import LogSink
from src.platform.logs.schemas import LogEvent, LogLevel
from src.platform.logs.service import LogService, merge_emit_component_trace_fields
from src.platform.orchestrator.step_scoped_logs import StepScopedLogService


class _CapturingSink(LogSink):
    """Test sink that records every emitted event in memory."""

    def __init__(self) -> None:
        self.events: list[LogEvent] = []

    async def emit(self, event: LogEvent) -> None:
        self.events.append(event)


def _make_base() -> tuple[LogService, _CapturingSink]:
    sink = _CapturingSink()
    return LogService(sink=sink), sink


@pytest.mark.asyncio
async def test_emit_safe_injects_step_target_when_payload_lacks_participants() -> None:
    base, sink = _make_base()
    step_run_id = uuid.uuid4()
    scoped = StepScopedLogService(base, step_run_id=step_run_id, component_id='access_plan')

    # Use the async path so we can deterministically await the emit.
    await scoped.emit_log(
        level=LogLevel.INFO,
        message='step started',
        component='access_plan',
        payload={'plan_id': 'p-1'},
    )

    assert len(sink.events) == 1
    ev = sink.events[0]
    assert ev.target_id == str(step_run_id)
    assert ev.target_type == 'system'
    assert ev.initiator_id == 'access_plan'
    assert ev.actor_id == 'access_plan'
    assert ev.payload['plan_id'] == 'p-1'


@pytest.mark.asyncio
async def test_emit_log_respects_caller_supplied_participants() -> None:
    base, sink = _make_base()
    step_run_id = uuid.uuid4()
    scoped = StepScopedLogService(base, step_run_id=step_run_id, component_id='access_plan')
    explicit_target_id = 'application-abc'

    await scoped.emit_log(
        level=LogLevel.INFO,
        message='account touched',
        component='access_apply',
        payload=merge_emit_component_trace_fields(
            {'plan_id': 'p-1'},
            component_id='access_apply',
            target_id=explicit_target_id,
            target_type='application',
        ),
    )

    assert len(sink.events) == 1
    ev = sink.events[0]
    # Caller's explicit targeting wins — wrapper must not overwrite to step_run_id.
    assert ev.target_id == explicit_target_id
    assert ev.target_type == 'application'
    assert ev.target_id != str(step_run_id)


@pytest.mark.asyncio
async def test_emit_log_no_payload_still_emits_with_step_target() -> None:
    """Action handlers often call emit_log(...) without any payload.
    The wrapper must build a participant block so the underlying service
    does not silently drop the line."""
    base, sink = _make_base()
    step_run_id = uuid.uuid4()
    scoped = StepScopedLogService(base, step_run_id=step_run_id, component_id='notifications')

    await scoped.emit_log(
        level=LogLevel.DEBUG,
        message='sending email',
        component='notifications',
    )

    assert len(sink.events) == 1
    assert sink.events[0].target_id == str(step_run_id)
    assert sink.events[0].initiator_id == 'notifications'


@pytest.mark.asyncio
async def test_emit_log_with_other_extras_preserved() -> None:
    base, sink = _make_base()
    step_run_id = uuid.uuid4()
    scoped = StepScopedLogService(base, step_run_id=step_run_id, component_id='access_apply')

    await scoped.emit_log(
        level=LogLevel.INFO,
        message='apply tick',
        component='access_apply',
        payload={'item_id': 'i-1'},
        request_id='req-7',
    )

    assert len(sink.events) == 1
    ev = sink.events[0]
    assert ev.target_id == str(step_run_id)
    assert ev.payload['item_id'] == 'i-1'
    assert ev.payload.get('request_id') == 'req-7'


@pytest.mark.asyncio
async def test_step_run_id_is_stamped_into_payload_even_when_caller_targets_something_else() -> None:
    """The per-step UI filter queries payload->>'step_run_id', so the side
    channel must be present regardless of what target_id the caller chose."""
    base, sink = _make_base()
    step_run_id = uuid.uuid4()
    scoped = StepScopedLogService(base, step_run_id=step_run_id, component_id='access_apply')

    await scoped.emit_log(
        level=LogLevel.WARNING,
        message='item_connector_error',
        component='access_apply',
        payload=merge_emit_component_trace_fields(
            {'plan_id': 'p-1', 'item_id': 'i-1', 'error': 'boom'},
            component_id='access_apply',
            target_id='plan-abc',
        ),
    )

    assert len(sink.events) == 1
    ev = sink.events[0]
    # Caller's explicit plan target is preserved.
    assert ev.target_id == 'plan-abc'
    # AND the step_run_id is stamped in payload for step-scoped filtering.
    assert ev.payload['step_run_id'] == str(step_run_id)
    assert ev.payload['plan_id'] == 'p-1'


@pytest.mark.asyncio
async def test_step_run_id_caller_override_not_clobbered() -> None:
    """If the caller already set payload['step_run_id'] (e.g. matcher emit),
    the wrapper must not overwrite it."""
    base, sink = _make_base()
    runner_step_id = uuid.uuid4()
    explicit_step_id = uuid.uuid4()
    scoped = StepScopedLogService(base, step_run_id=runner_step_id, component_id='access_apply')

    await scoped.emit_log(
        level=LogLevel.INFO,
        message='matcher woke step',
        component='access_apply',
        payload={'step_run_id': str(explicit_step_id), 'detail': 'x'},
    )

    assert len(sink.events) == 1
    assert sink.events[0].payload['step_run_id'] == str(explicit_step_id)

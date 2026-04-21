# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapturingEventService contract tests (5 tests)."""

from datetime import UTC, datetime
import uuid

from src.platform.events.interface import EventSink
from src.platform.events.schemas import EventEnvelope
from src.platform.events.testing import CapturingEventService


def _make_envelope(event_type: str = 'inventory.access_fact.created') -> EventEnvelope:
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type=event_type,
        occurred_at=datetime.now(UTC),
        correlation_id=str(uuid.uuid4()),
    )


# ---------------------------------------------------------------------------
# 1. emit appends envelope
# ---------------------------------------------------------------------------


async def test_emit_appends_envelope() -> None:
    svc = CapturingEventService()
    env = _make_envelope()
    await svc.emit(env)
    assert svc.emitted == [env]


# ---------------------------------------------------------------------------
# 2. emit preserves call order
# ---------------------------------------------------------------------------


async def test_emit_preserves_order() -> None:
    svc = CapturingEventService()
    e1 = _make_envelope()
    e2 = _make_envelope()
    e3 = _make_envelope()
    await svc.emit(e1)
    await svc.emit(e2)
    await svc.emit(e3)
    assert svc.emitted == [e1, e2, e3]
    assert svc.emitted[0].event_id == e1.event_id
    assert svc.emitted[1].event_id == e2.event_id
    assert svc.emitted[2].event_id == e3.event_id


# ---------------------------------------------------------------------------
# 3. filter_by_type returns exact matches only
# ---------------------------------------------------------------------------


async def test_filter_by_type_returns_exact_matches() -> None:
    svc = CapturingEventService()
    e_abc1 = _make_envelope('a.b.c')
    e_abd = _make_envelope('a.b.d')
    e_abc2 = _make_envelope('a.b.c')

    await svc.emit(e_abc1)
    await svc.emit(e_abd)
    await svc.emit(e_abc2)

    result = svc.filter_by_type('a.b.c')
    assert len(result) == 2
    assert result[0].event_id == e_abc1.event_id
    assert result[1].event_id == e_abc2.event_id


# ---------------------------------------------------------------------------
# 4. clear empties emitted list
# ---------------------------------------------------------------------------


async def test_clear_empties_emitted() -> None:
    svc = CapturingEventService()
    await svc.emit(_make_envelope())
    svc.clear()
    assert svc.emitted == []


# ---------------------------------------------------------------------------
# 5. CapturingEventService satisfies EventSink protocol
# ---------------------------------------------------------------------------


def test_capturing_service_satisfies_event_sink_protocol() -> None:
    svc = CapturingEventService()
    assert isinstance(svc, EventSink)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for mq_eas_projection_consumer.handler (T6-T9).

No live MQ, no live DB. Uses mock session_factory and stub service.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid
from uuid import UUID

import pytest
from src.engines.access_effective.schemas import IncrementalApplyKind
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import noop_event_service
from src.platform.logs.schemas import LogLevel
from src.runtimes.mq_eas_projection_consumer.handler import handle_message

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_T = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_EID = uuid.UUID('11111111-1111-1111-1111-111111111111')
_CID = str(uuid.UUID('22222222-2222-2222-2222-222222222222'))
_FID = uuid.UUID('33333333-3333-3333-3333-333333333333')


# ---------------------------------------------------------------------------
# CapturingLogService — slice-local duck-typed fake
# ---------------------------------------------------------------------------


class CapturingLogService:
    """Minimal fake that captures every emit_safe call."""

    def __init__(self) -> None:
        self.events: list[tuple[LogLevel, str, str, dict[str, Any] | None, dict[str, Any]]] = []

    def emit_safe(
        self,
        level: LogLevel = LogLevel.INFO,
        message: str = '',
        component: str = '',
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.events.append((level, message, component, payload, kwargs))

    def emit_log(
        self,
        level: LogLevel = LogLevel.INFO,
        message: str = '',
        component: str = '',
        payload: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        self.events.append((level, message, component, payload, kwargs))

    def emit_event_safe(self, event: Any) -> None:
        pass

    def emit_event(self, event: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event_body(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: UUID = _EID,
    correlation_id: str = _CID,
    occurred_at: datetime = _T,
) -> bytes:
    """Construct a JSON-encoded EventEnvelope body for testing."""
    envelope = EventEnvelope(
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        correlation_id=correlation_id,
        payload=payload,
        initiator_kind=EventParticipantKind.SYSTEM,
        initiator_id='test-initiator',
        actor_kind=EventParticipantKind.SYSTEM,
        actor_id='test-actor',
        target_kind=EventParticipantKind.SYSTEM,
        target_id='test-target',
    )
    return json.dumps(envelope.model_dump(mode='json')).encode('utf-8')


def _make_stub_session_factory(stub_session: Any) -> Any:
    """Return an async_sessionmaker-compatible mock."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=stub_session)
    cm.__aexit__ = AsyncMock(return_value=None)
    factory = MagicMock(return_value=cm)
    return factory


def _make_stub_service() -> MagicMock:
    """Return a stub EffectiveAccessProjectionService with AsyncMock apply."""
    stub = MagicMock()
    stub.apply_incremental_change = AsyncMock(return_value=None)
    return stub


# ---------------------------------------------------------------------------
# T6 — happy path: inventory.access_fact.created maps to UPSERT
# ---------------------------------------------------------------------------


def test_handler_decodes_access_fact_created_and_calls_apply() -> None:
    """T6: inventory.access_fact.created body maps to UPSERT apply call with correct kwargs."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    body = _make_event_body(
        'inventory.access_fact.created',
        {'access_fact_id': str(_FID), 'subject_id': str(uuid.uuid4())},
    )

    handle_message(
        body,
        routing_key='inventory.access_fact.created',
        session_factory=session_factory,
        projection_service_factory=lambda session, es: stub_svc,
        log_service=log,  # type: ignore[arg-type]
        event_service=noop_event_service,
    )

    stub_svc.apply_incremental_change.assert_called_once()
    call_kwargs = stub_svc.apply_incremental_change.call_args.kwargs
    assert call_kwargs['access_fact_id'] == _FID
    assert call_kwargs['change_kind'] is IncrementalApplyKind.UPSERT
    assert call_kwargs['observed_at'] == _T
    assert call_kwargs['correlation_id'] == UUID(_CID)
    assert call_kwargs['causation_event_id'] == _EID

    stub_session.commit.assert_called_once()
    stub_session.rollback.assert_not_called()

    error_events = [e for e in log.events if e[0] == LogLevel.ERROR]
    assert error_events == []


# ---------------------------------------------------------------------------
# T7 — inventory.access_fact.revoked maps to INVALIDATE_FACT
# ---------------------------------------------------------------------------

_IID = uuid.UUID('44444444-4444-4444-4444-444444444444')


def test_access_fact_revoked_maps_to_invalidate_fact() -> None:
    """T7: inventory.access_fact.revoked maps to INVALIDATE_FACT with fact_id payload key."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    body = _make_event_body(
        'inventory.access_fact.revoked',
        {
            'fact_id': str(_FID),
            'subject_id': str(uuid.uuid4()),
            'resource_id': str(uuid.uuid4()),
            'action_id': 1,
            'action_slug': 'read',
            'revoked_at': '2026-01-01T00:00:00+00:00',
        },
    )

    handle_message(
        body,
        routing_key='inventory.access_fact.revoked',
        session_factory=session_factory,
        projection_service_factory=lambda session, es: stub_svc,
        log_service=log,  # type: ignore[arg-type]
        event_service=noop_event_service,
    )

    stub_svc.apply_incremental_change.assert_called_once()
    call_kwargs = stub_svc.apply_incremental_change.call_args.kwargs
    assert call_kwargs['change_kind'] is IncrementalApplyKind.INVALIDATE_FACT
    assert call_kwargs['access_fact_id'] == _FID
    assert call_kwargs.get('initiative_id') is None

    stub_session.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# H-I1 — inventory.initiative.expired maps to INVALIDATE_INITIATIVE
# ---------------------------------------------------------------------------


def test_initiative_expired_maps_to_invalidate_initiative() -> None:
    """H-I1: inventory.initiative.expired maps to INVALIDATE_INITIATIVE with initiative_id."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    body = _make_event_body(
        'inventory.initiative.expired',
        {'initiative_id': str(_IID), 'access_fact_id': str(_FID), 'at': '2026-01-01T00:00:00+00:00'},
    )

    handle_message(
        body,
        routing_key='inventory.initiative.expired',
        session_factory=session_factory,
        projection_service_factory=lambda session, es: stub_svc,
        log_service=log,  # type: ignore[arg-type]
        event_service=noop_event_service,
    )

    stub_svc.apply_incremental_change.assert_called_once()
    call_kwargs = stub_svc.apply_incremental_change.call_args.kwargs
    assert call_kwargs['change_kind'] is IncrementalApplyKind.INVALIDATE_INITIATIVE
    assert call_kwargs['initiative_id'] == _IID
    # access_fact_id must NOT be passed to the service
    assert call_kwargs.get('access_fact_id') is None

    stub_session.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# H-I2 — inventory.initiative.expired with missing initiative_id is a silent skip
# ---------------------------------------------------------------------------


def test_initiative_expired_missing_initiative_id_is_silent_skip() -> None:
    """H-I2: inventory.initiative.expired without initiative_id in payload → WARNING, no apply call."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    # Payload deliberately missing initiative_id
    body = _make_event_body(
        'inventory.initiative.expired',
        {'access_fact_id': str(_FID), 'at': '2026-01-01T00:00:00+00:00'},
    )

    handle_message(
        body,
        routing_key='inventory.initiative.expired',
        session_factory=session_factory,
        projection_service_factory=lambda session, es: stub_svc,
        log_service=log,  # type: ignore[arg-type]
        event_service=noop_event_service,
    )

    stub_svc.apply_incremental_change.assert_not_called()

    warning_events = [e for e in log.events if e[0] == LogLevel.WARNING]
    assert len(warning_events) == 1

    error_events = [e for e in log.events if e[0] == LogLevel.ERROR]
    assert error_events == []


# ---------------------------------------------------------------------------
# T8 — unrelated routing keys are silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('event_type', ['inventory.access_fact.retrieved', 'inventory.subject.created'])
def test_handler_skips_unrelated_events(event_type: str) -> None:
    """T8: events not in the relevant set are silently dropped (no apply, no error log)."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    # Use a minimal but valid EventEnvelope body via inventory.access_fact.created
    # then override routing_key to simulate an over-permissive binding delivering
    # an envelope with a non-relevant event_type.
    # Actually we need both routing_key and envelope event_type to be consistent
    # to pass the mismatch guard, so we must use an envelope with the same event_type.
    # But these event_types don't match the 3-segment pattern required by EventEnvelope.
    # We pass a JSON body that fails envelope validation => parse_error.
    # Instead, pass routing_key not in relevant set with a valid non-relevant envelope.
    # Both event_types ARE 3-segment, so we can construct valid envelopes for them.
    body = _make_event_body(
        event_type,
        {'access_fact_id': str(_FID)},
    )

    handle_message(
        body,
        routing_key=event_type,
        session_factory=session_factory,
        projection_service_factory=lambda session, es: stub_svc,
        log_service=log,  # type: ignore[arg-type]
        event_service=noop_event_service,
    )

    stub_svc.apply_incremental_change.assert_not_called()
    assert log.events == []


# ---------------------------------------------------------------------------
# New — test_handler_rejects_non_relevant_routing_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    'routing_key',
    [
        'inventory.access_fact.retrieved',
        'inventory.access_fact.updated',
        'inventory.subject.created',
    ],
)
def test_handler_rejects_non_relevant_routing_key(routing_key: str) -> None:
    """Routing key not in _EVENT_TYPES_RELEVANT → silent skip (no apply, no error log)."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    # Build envelope with matching event_type to pass mismatch guard
    body = _make_event_body(
        routing_key,
        {'access_fact_id': str(_FID)},
    )

    handle_message(
        body,
        routing_key=routing_key,
        session_factory=session_factory,
        projection_service_factory=lambda session, es: stub_svc,
        log_service=log,  # type: ignore[arg-type]
        event_service=noop_event_service,
    )

    stub_svc.apply_incremental_change.assert_not_called()
    assert log.events == []


# ---------------------------------------------------------------------------
# New — test_handler_rejects_routing_key_mismatch (C1a)
# ---------------------------------------------------------------------------


def test_handler_rejects_routing_key_mismatch() -> None:
    """C1a: routing_key != envelope.event_type → WARNING log, no apply call."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    # Envelope event_type = initiative.created, but routing_key = access_fact.created
    body = _make_event_body(
        'inventory.initiative.created',
        {'initiative_id': str(_IID), 'access_fact_id': str(_FID)},
    )

    handle_message(
        body,
        routing_key='inventory.access_fact.created',
        session_factory=session_factory,
        projection_service_factory=lambda session, es: stub_svc,
        log_service=log,  # type: ignore[arg-type]
        event_service=noop_event_service,
    )

    stub_svc.apply_incremental_change.assert_not_called()

    mismatch_events = [e for e in log.events if e[0] == LogLevel.WARNING]
    assert len(mismatch_events) == 1

    error_events = [e for e in log.events if e[0] == LogLevel.ERROR]
    assert error_events == []


# ---------------------------------------------------------------------------
# T9 — malformed payloads handled gracefully
# ---------------------------------------------------------------------------


def test_handler_ignores_malformed_payload_non_json() -> None:
    """T9a: non-JSON body → parse_error at ERROR, apply not called."""
    stub_svc = _make_stub_service()
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    handle_message(
        b'not json',
        routing_key='inventory.access_fact.created',
        session_factory=session_factory,
        projection_service_factory=lambda session, es: stub_svc,
        log_service=log,  # type: ignore[arg-type]
        event_service=noop_event_service,
    )

    stub_svc.apply_incremental_change.assert_not_called()
    parse_error_events = [e for e in log.events if e[0] == LogLevel.ERROR]
    assert len(parse_error_events) == 1


def test_handler_ignores_malformed_payload_invalid_envelope() -> None:
    """T9b: JSON but not a valid EventEnvelope → parse_error at ERROR, apply not called."""
    stub_svc = _make_stub_service()
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    handle_message(
        b'{"foo": 1}',
        routing_key='inventory.access_fact.created',
        session_factory=session_factory,
        projection_service_factory=lambda session, es: stub_svc,
        log_service=log,  # type: ignore[arg-type]
        event_service=noop_event_service,
    )

    stub_svc.apply_incremental_change.assert_not_called()
    parse_error_events = [e for e in log.events if e[0] == LogLevel.ERROR]
    assert len(parse_error_events) == 1


def test_handler_ignores_missing_access_fact_id() -> None:
    """T9c: valid envelope for inventory.access_fact.created but payload missing access_fact_id → WARNING."""
    stub_svc = _make_stub_service()
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    body = _make_event_body(
        'inventory.access_fact.created',
        {'subject_id': str(uuid.uuid4())},  # no access_fact_id
    )

    handle_message(
        body,
        routing_key='inventory.access_fact.created',
        session_factory=session_factory,
        projection_service_factory=lambda session, es: stub_svc,
        log_service=log,  # type: ignore[arg-type]
        event_service=noop_event_service,
    )

    stub_svc.apply_incremental_change.assert_not_called()
    missing_events = [e for e in log.events if e[0] == LogLevel.WARNING]
    assert len(missing_events) == 1

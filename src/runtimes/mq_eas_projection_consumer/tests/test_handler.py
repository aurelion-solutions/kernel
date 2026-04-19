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
from src.capabilities.effective_access.schemas import IncrementalApplyKind
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
        self.events: list[tuple[str, LogLevel, str, str, dict[str, Any], dict[str, Any]]] = []

    def emit_safe(
        self,
        event_type: str,
        level: LogLevel,
        message: str,
        component: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        self.events.append((event_type, level, message, component, payload, kwargs))

    def emit_log(
        self,
        event_type: str,
        level: LogLevel,
        message: str,
        component: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        self.events.append((event_type, level, message, component, payload, kwargs))

    def emit_event_safe(self, event: Any) -> None:
        pass

    def emit_event(self, event: Any) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mq_body(
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: UUID = _EID,
    correlation_id: str = _CID,
    timestamp: datetime = _T,
) -> bytes:
    """Construct a JSON-encoded LogEvent body for testing."""
    data = {
        'event_id': str(event_id),
        'event_type': event_type,
        'timestamp': timestamp.isoformat(),
        'level': 'info',
        'message': 'test event',
        'component': 'inventory.access_facts',
        'correlation_id': correlation_id,
        'payload': payload,
        'initiator_type': 'system',
        'initiator_id': 'test-initiator',
        'actor_type': 'system',
        'actor_id': 'test-actor',
        'target_type': 'system',
        'target_id': 'test-target',
    }
    return json.dumps(data).encode('utf-8')


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
# T6 — happy path: access_fact.created maps to UPSERT
# ---------------------------------------------------------------------------


def test_handler_decodes_access_fact_created_and_calls_apply() -> None:
    """T6: access_fact.created body maps to UPSERT apply call with correct kwargs."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    body = _make_mq_body(
        'access_fact.created',
        {'access_fact_id': str(_FID), 'subject_id': str(uuid.uuid4())},
    )

    handle_message(
        body,
        session_factory=session_factory,
        projection_service_factory=lambda session, ls: stub_svc,
        log_service=log,  # type: ignore[arg-type]
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

    error_events = [e for e in log.events if e[1] == LogLevel.ERROR]
    assert error_events == []


# ---------------------------------------------------------------------------
# T7 — access_fact.invalidated maps to INVALIDATE_FACT (rewritten for Step 6a)
# ---------------------------------------------------------------------------

_IID = uuid.UUID('44444444-4444-4444-4444-444444444444')


def test_access_fact_invalidated_maps_to_invalidate_fact() -> None:
    """T7: access_fact.invalidated maps to INVALIDATE_FACT with access_fact_id."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    body = _make_mq_body(
        'access_fact.invalidated',
        {'access_fact_id': str(_FID), 'at': '2026-01-01T00:00:00+00:00'},
    )

    handle_message(
        body,
        session_factory=session_factory,
        projection_service_factory=lambda session, ls: stub_svc,
        log_service=log,  # type: ignore[arg-type]
    )

    stub_svc.apply_incremental_change.assert_called_once()
    call_kwargs = stub_svc.apply_incremental_change.call_args.kwargs
    assert call_kwargs['change_kind'] is IncrementalApplyKind.INVALIDATE_FACT
    assert call_kwargs['access_fact_id'] == _FID
    assert call_kwargs.get('initiative_id') is None

    stub_session.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# H-I1 — initiative.expired maps to INVALIDATE_INITIATIVE (Step 6a)
# ---------------------------------------------------------------------------


def test_initiative_expired_maps_to_invalidate_initiative() -> None:
    """H-I1: initiative.expired maps to INVALIDATE_INITIATIVE with initiative_id."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    body = _make_mq_body(
        'initiative.expired',
        {'initiative_id': str(_IID), 'access_fact_id': str(_FID), 'at': '2026-01-01T00:00:00+00:00'},
    )

    handle_message(
        body,
        session_factory=session_factory,
        projection_service_factory=lambda session, ls: stub_svc,
        log_service=log,  # type: ignore[arg-type]
    )

    stub_svc.apply_incremental_change.assert_called_once()
    call_kwargs = stub_svc.apply_incremental_change.call_args.kwargs
    assert call_kwargs['change_kind'] is IncrementalApplyKind.INVALIDATE_INITIATIVE
    assert call_kwargs['initiative_id'] == _IID
    # access_fact_id must NOT be passed to the service
    assert call_kwargs.get('access_fact_id') is None

    stub_session.rollback.assert_not_called()


# ---------------------------------------------------------------------------
# H-I2 — initiative.expired with missing initiative_id is a silent skip (Step 6a)
# ---------------------------------------------------------------------------


def test_initiative_expired_missing_initiative_id_is_silent_skip() -> None:
    """H-I2: initiative.expired without initiative_id in payload → WARNING, no apply call."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    # Payload deliberately missing initiative_id
    body = _make_mq_body(
        'initiative.expired',
        {'access_fact_id': str(_FID), 'at': '2026-01-01T00:00:00+00:00'},
    )

    handle_message(
        body,
        session_factory=session_factory,
        projection_service_factory=lambda session, ls: stub_svc,
        log_service=log,  # type: ignore[arg-type]
    )

    stub_svc.apply_incremental_change.assert_not_called()

    warning_events = [e for e in log.events if e[0] == 'eas.projection.consumer.missing_initiative_id']
    assert len(warning_events) == 1
    assert warning_events[0][1] == LogLevel.WARNING

    error_events = [e for e in log.events if e[1] == LogLevel.ERROR]
    assert error_events == []


# ---------------------------------------------------------------------------
# T8 — unrelated events are silently skipped
# ---------------------------------------------------------------------------


@pytest.mark.parametrize('event_type', ['access_fact.retrieved', 'subject.created'])
def test_handler_skips_unrelated_events(event_type: str) -> None:
    """T8: events not in the relevant set are silently dropped (no apply, no error log)."""
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()
    stub_svc = _make_stub_service()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    body = _make_mq_body(
        event_type,
        {'access_fact_id': str(_FID)},
    )

    handle_message(
        body,
        session_factory=session_factory,
        projection_service_factory=lambda session, ls: stub_svc,
        log_service=log,  # type: ignore[arg-type]
    )

    stub_svc.apply_incremental_change.assert_not_called()
    assert log.events == []


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
        session_factory=session_factory,
        projection_service_factory=lambda session, ls: stub_svc,
        log_service=log,  # type: ignore[arg-type]
    )

    stub_svc.apply_incremental_change.assert_not_called()
    parse_error_events = [e for e in log.events if e[0] == 'eas.projection.consumer.parse_error']
    assert len(parse_error_events) == 1
    assert parse_error_events[0][1] == LogLevel.ERROR


def test_handler_ignores_malformed_payload_invalid_log_event() -> None:
    """T9b: JSON but not a valid LogEvent → parse_error at ERROR, apply not called."""
    stub_svc = _make_stub_service()
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    handle_message(
        b'{"foo": 1}',
        session_factory=session_factory,
        projection_service_factory=lambda session, ls: stub_svc,
        log_service=log,  # type: ignore[arg-type]
    )

    stub_svc.apply_incremental_change.assert_not_called()
    parse_error_events = [e for e in log.events if e[0] == 'eas.projection.consumer.parse_error']
    assert len(parse_error_events) == 1
    assert parse_error_events[0][1] == LogLevel.ERROR


def test_handler_ignores_missing_access_fact_id() -> None:
    """T9c: valid LogEvent for access_fact.created but payload missing access_fact_id → missing_fact_id WARNING."""
    stub_svc = _make_stub_service()
    stub_session = MagicMock()
    stub_session.commit = AsyncMock()
    stub_session.rollback = AsyncMock()

    session_factory = _make_stub_session_factory(stub_session)
    log = CapturingLogService()

    body = _make_mq_body(
        'access_fact.created',
        {'subject_id': str(uuid.uuid4())},  # no access_fact_id
    )

    handle_message(
        body,
        session_factory=session_factory,
        projection_service_factory=lambda session, ls: stub_svc,
        log_service=log,  # type: ignore[arg-type]
    )

    stub_svc.apply_incremental_change.assert_not_called()
    missing_events = [e for e in log.events if e[0] == 'eas.projection.consumer.missing_fact_id']
    assert len(missing_events) == 1
    assert missing_events[0][1] == LogLevel.WARNING

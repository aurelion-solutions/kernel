# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EventEnvelope schema contract tests (16 tests)."""

from datetime import UTC, datetime, timezone
import uuid

from pydantic import ValidationError
import pytest
from src.platform.events.schemas import EventEnvelope, EventParticipantKind


def _make_envelope(**overrides: object) -> EventEnvelope:
    """Return a valid EventEnvelope, optionally overriding fields."""
    defaults: dict[str, object] = {
        'event_id': uuid.uuid4(),
        'event_type': 'inventory.access_fact.created',
        'occurred_at': datetime.now(UTC),
        'correlation_id': str(uuid.uuid4()),
        'causation_id': None,
        'payload': {'key': 'value'},
        'initiator_kind': EventParticipantKind.USER,
        'initiator_id': 'user-1',
        'actor_kind': EventParticipantKind.SYSTEM,
        'actor_id': 'kernel',
        'target_kind': EventParticipantKind.APPLICATION,
        'target_id': 'app-42',
    }
    defaults.update(overrides)
    return EventEnvelope(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 1. round-trip JSON
# ---------------------------------------------------------------------------


def test_envelope_round_trip_json() -> None:
    original = _make_envelope()
    json_str = original.model_dump_json()
    reconstructed = EventEnvelope.model_validate_json(json_str)
    assert reconstructed == original


# ---------------------------------------------------------------------------
# 2-7. event_type validation
# ---------------------------------------------------------------------------


def test_event_type_accepts_three_segment_lowercase() -> None:
    env = _make_envelope(event_type='inventory.access_fact.created')
    assert env.event_type == 'inventory.access_fact.created'


def test_event_type_rejects_two_segment() -> None:
    with pytest.raises(ValidationError):
        _make_envelope(event_type='inventory.created')


def test_event_type_rejects_four_segment() -> None:
    with pytest.raises(ValidationError):
        _make_envelope(event_type='a.b.c.d')


def test_event_type_rejects_uppercase() -> None:
    with pytest.raises(ValidationError):
        _make_envelope(event_type='Inventory.Access.Created')


def test_event_type_rejects_empty_segment() -> None:
    with pytest.raises(ValidationError):
        _make_envelope(event_type='inventory..created')


def test_event_type_rejects_dash() -> None:
    with pytest.raises(ValidationError):
        _make_envelope(event_type='inventory.access-fact.created')


# ---------------------------------------------------------------------------
# 8-9. occurred_at validation
# ---------------------------------------------------------------------------


def test_occurred_at_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        _make_envelope(occurred_at=datetime.utcnow())  # noqa: DTZ003


def test_occurred_at_accepts_utc_aware() -> None:
    ts = datetime.now(UTC)
    env = _make_envelope(occurred_at=ts)
    assert env.occurred_at.tzinfo is not None


# ---------------------------------------------------------------------------
# 10. schema_version default
# ---------------------------------------------------------------------------


def test_schema_version_defaults_to_1() -> None:
    env = _make_envelope()
    assert env.schema_version == '1'


# ---------------------------------------------------------------------------
# 11. extra='forbid'
# ---------------------------------------------------------------------------


def test_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope(
            event_id=uuid.uuid4(),
            event_type='a.b.c',
            occurred_at=datetime.now(UTC),
            correlation_id=str(uuid.uuid4()),
            unknown_field='x',  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# 12. frozen=True
# ---------------------------------------------------------------------------


def test_envelope_is_frozen() -> None:
    env = _make_envelope()
    with pytest.raises(ValidationError):
        env.schema_version = '2'  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 13. causation_id self-referential guard
# ---------------------------------------------------------------------------


def test_causation_not_self_referential() -> None:
    eid = uuid.uuid4()
    with pytest.raises(ValidationError):
        _make_envelope(event_id=eid, causation_id=eid)


# ---------------------------------------------------------------------------
# Extra: correlation_id coercion
# ---------------------------------------------------------------------------


def test_correlation_id_accepts_uuid_object() -> None:
    cid = uuid.uuid4()
    env = _make_envelope(correlation_id=cid)
    assert env.correlation_id == str(cid)


def test_correlation_id_rejects_empty_string() -> None:
    with pytest.raises(ValidationError):
        _make_envelope(correlation_id='   ')


# ---------------------------------------------------------------------------
# Extra: non-UTC aware datetime is still accepted (tz-aware but not UTC)
# ---------------------------------------------------------------------------


def test_occurred_at_accepts_non_utc_aware_datetime() -> None:
    """Any tz-aware datetime is accepted — only naive is rejected."""
    import datetime as dt

    tz = timezone(dt.timedelta(hours=3))
    ts = datetime.now(tz)
    env = _make_envelope(occurred_at=ts)
    assert env.occurred_at.tzinfo is not None

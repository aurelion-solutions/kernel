# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for MitigationService.create."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.inventory.assessment.mitigation_controls.models import MitigationControl, MitigationControlType
from src.inventory.assessment.mitigations.exceptions import (
    MitigationControlInactiveError,
    MitigationControlNotFoundError,
    MitigationDuplicateActiveError,
    MitigationInvalidInitialStatusError,
    MitigationOwnerNotFoundError,
    MitigationRuleNotFoundError,
    MitigationRuleNotMitigatableError,
    MitigationScopePairError,
    MitigationSubjectNotFoundError,
    MitigationValidWindowError,
)
from src.inventory.assessment.mitigations.models import MitigationStatus
from src.inventory.assessment.mitigations.schemas import MitigationCreate
from src.inventory.assessment.mitigations.service import MitigationService
from src.inventory.policy.sod_rules.models import SodRule, SodRuleScope, SodSeverity
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(session, capturing: CapturingEventService) -> MitigationService:
    event_service = EventService(sink=capturing)
    return MitigationService(session, NoOpLogService(), event_service)


async def _insert_rule(session, *, mitigation_allowed: bool = True) -> SodRule:
    rule = SodRule(
        code=f'RULE-{uuid.uuid4().hex[:6]}',
        name='Test Rule',
        severity=SodSeverity.high,
        scope_mode=SodRuleScope.global_,
        mitigation_allowed=mitigation_allowed,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def _insert_control(session, *, is_active: bool = True) -> MitigationControl:
    ctrl = MitigationControl(
        code=f'CTRL-{uuid.uuid4().hex[:6]}',
        name='Test Control',
        type=MitigationControlType.attestation,
        is_active=is_active,
    )
    session.add(ctrl)
    await session.flush()
    await session.refresh(ctrl)
    return ctrl


async def _insert_subject(session) -> uuid.UUID:
    from src.inventory.nhi.repository import create_nhi
    from src.inventory.subjects.models import SubjectKind, SubjectNHIKind, SubjectNHIStatus
    from src.inventory.subjects.repository import create_subject

    nhi = await create_nhi(
        session,
        external_id=f'tst-nhi-{uuid.uuid4().hex[:8]}',
        name='Test NHI',
        kind='service_account',
    )
    subject = await create_subject(
        session,
        external_id=f'tst-subj-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status=SubjectNHIStatus.active,
    )
    return subject.id


def _now() -> datetime:
    return datetime.now(UTC)


def _create_payload(
    rule: SodRule, ctrl: MitigationControl, subject_id: uuid.UUID, owner_id: uuid.UUID, **kwargs
) -> MitigationCreate:
    defaults: dict = {
        'rule_id': rule.id,
        'control_id': ctrl.id,
        'subject_id': subject_id,
        'owner_id': owner_id,
        'valid_from': _now(),
        'status': MitigationStatus.proposed,
    }
    defaults.update(kwargs)
    return MitigationCreate(**defaults)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_happy_path_proposed(session_factory) -> None:
    """Happy path: create proposed mitigation returns MitigationRead + one mitigation.created event."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = _create_payload(rule, ctrl, subject_id, owner_id)
        result = await svc.create(payload, correlation_id='test-corr-1')
        await session.commit()

    assert result.id is not None
    assert result.status == MitigationStatus.proposed
    events = capturing.filter_by_type('access_analysis.mitigation.created')
    assert len(events) == 1
    assert events[0].payload['status'] == 'proposed'
    assert events[0].payload['mitigation_id'] == str(result.id)


@pytest.mark.asyncio
async def test_create_with_active_status_emits_only_created(session_factory) -> None:
    """Create with status=active emits only mitigation.created, NOT mitigation.activated."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = _create_payload(rule, ctrl, subject_id, owner_id, status=MitigationStatus.active)
        result = await svc.create(payload)
        await session.commit()

    assert result.status == MitigationStatus.active
    assert len(capturing.filter_by_type('access_analysis.mitigation.created')) == 1
    assert len(capturing.filter_by_type('access_analysis.mitigation.activated')) == 0


@pytest.mark.asyncio
async def test_create_with_expired_raises_invalid_initial_status(session_factory) -> None:
    """Create with status=expired raises MitigationInvalidInitialStatusError; nothing flushed."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = _create_payload(rule, ctrl, subject_id, owner_id, status=MitigationStatus.expired)
        with pytest.raises(MitigationInvalidInitialStatusError):
            await svc.create(payload)

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_with_revoked_raises_invalid_initial_status(session_factory) -> None:
    """Create with status=revoked raises MitigationInvalidInitialStatusError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = _create_payload(rule, ctrl, subject_id, owner_id, status=MitigationStatus.revoked)
        with pytest.raises(MitigationInvalidInitialStatusError):
            await svc.create(payload)

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_rule_mitigation_not_allowed_raises(session_factory) -> None:
    """Rule with mitigation_allowed=False raises MitigationRuleNotMitigatableError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session, mitigation_allowed=False)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = _create_payload(rule, ctrl, subject_id, owner_id)
        with pytest.raises(MitigationRuleNotMitigatableError):
            await svc.create(payload)

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_rule_not_found_raises(session_factory) -> None:
    """Non-existent rule_id raises MitigationRuleNotFoundError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = MitigationCreate(
            rule_id=99999,
            control_id=ctrl.id,
            subject_id=subject_id,
            owner_id=owner_id,
            valid_from=_now(),
            status=MitigationStatus.proposed,
        )
        with pytest.raises(MitigationRuleNotFoundError):
            await svc.create(payload)

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_control_not_found_raises(session_factory) -> None:
    """Non-existent control_id raises MitigationControlNotFoundError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = MitigationCreate(
            rule_id=rule.id,
            control_id=99998,
            subject_id=subject_id,
            owner_id=owner_id,
            valid_from=_now(),
            status=MitigationStatus.proposed,
        )
        with pytest.raises(MitigationControlNotFoundError):
            await svc.create(payload)

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_control_inactive_raises(session_factory) -> None:
    """Inactive control raises MitigationControlInactiveError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session, is_active=False)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = _create_payload(rule, ctrl, subject_id, owner_id)
        with pytest.raises(MitigationControlInactiveError):
            await svc.create(payload)

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_subject_not_found_raises(session_factory) -> None:
    """Non-existent subject_id raises MitigationSubjectNotFoundError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = _create_payload(rule, ctrl, uuid.uuid4(), owner_id)
        with pytest.raises(MitigationSubjectNotFoundError):
            await svc.create(payload)

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_owner_not_found_raises(session_factory) -> None:
    """Non-existent owner_id raises MitigationOwnerNotFoundError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = _create_payload(rule, ctrl, subject_id, uuid.uuid4())
        with pytest.raises(MitigationOwnerNotFoundError):
            await svc.create(payload)

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_scope_pair_mismatch_raises(session_factory) -> None:
    """scope_key_id set, scope_value=None raises MitigationScopePairError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = _create_payload(rule, ctrl, subject_id, owner_id, scope_key_id=1, scope_value=None)
        with pytest.raises(MitigationScopePairError):
            await svc.create(payload)

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_valid_window_violation_raises(session_factory) -> None:
    """valid_until <= valid_from raises MitigationValidWindowError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        now = _now()
        payload = _create_payload(rule, ctrl, subject_id, owner_id, valid_from=now, valid_until=now)
        with pytest.raises(MitigationValidWindowError):
            await svc.create(payload)

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_duplicate_active_proposed_raises(session_factory) -> None:
    """Second proposed for same (rule, subject, unscoped) → MitigationDuplicateActiveError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        payload = _create_payload(rule, ctrl, subject_id, owner_id)
        await svc.create(payload, correlation_id='corr-1')
        await session.flush()

        payload2 = _create_payload(rule, ctrl, subject_id, owner_id)
        with pytest.raises(MitigationDuplicateActiveError):
            await svc.create(payload2, correlation_id='corr-2')

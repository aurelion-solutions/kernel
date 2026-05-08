# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for MitigationService.patch_status."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.inventory.assessment.mitigation_controls.models import MitigationControl, MitigationControlType
from src.inventory.assessment.mitigations.exceptions import (
    MitigationDuplicateActiveError,
    MitigationNotFoundError,
    MitigationReasonRequiredError,
    MitigationStatusTransitionError,
)
from src.inventory.assessment.mitigations.models import Mitigation, MitigationStatus
from src.inventory.assessment.mitigations.schemas import MitigationCreate, MitigationStatusPatch
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


async def _insert_rule(session) -> SodRule:
    rule = SodRule(
        code=f'RULE-{uuid.uuid4().hex[:6]}',
        name='Test Rule',
        severity=SodSeverity.high,
        scope_mode=SodRuleScope.global_,
        mitigation_allowed=True,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def _insert_control(session) -> MitigationControl:
    ctrl = MitigationControl(
        code=f'CTRL-{uuid.uuid4().hex[:6]}',
        name='Test Control',
        type=MitigationControlType.attestation,
        is_active=True,
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


async def _create_mitigation(
    svc: MitigationService,
    rule: SodRule,
    ctrl: MitigationControl,
    subject_id: uuid.UUID,
    owner_id: uuid.UUID,
    status: MitigationStatus = MitigationStatus.proposed,
) -> int:
    payload = MitigationCreate(
        rule_id=rule.id,
        control_id=ctrl.id,
        subject_id=subject_id,
        owner_id=owner_id,
        valid_from=_now(),
        status=status,
    )
    result = await svc.create(payload)
    return result.id


async def _insert_mitigation_direct(
    session,
    rule: SodRule,
    ctrl: MitigationControl,
    subject_id: uuid.UUID,
    owner_id: uuid.UUID,
    status: MitigationStatus,
) -> Mitigation:
    """Insert a mitigation directly (bypassing service) for terminal-state tests."""
    m = Mitigation(
        rule_id=rule.id,
        control_id=ctrl.id,
        subject_id=subject_id,
        owner_id=owner_id,
        valid_from=_now(),
        status=status,
    )
    session.add(m)
    await session.flush()
    await session.refresh(m)
    return m


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_proposed_to_active_emits_activated(session_factory) -> None:
    """proposed → active: status updated; one mitigation.activated event."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        mid = await _create_mitigation(svc, rule, ctrl, subject_id, owner_id)
        capturing.clear()

        result = await svc.patch_status(mid, MitigationStatusPatch(status=MitigationStatus.active))
        assert result.status == MitigationStatus.active

    events = capturing.filter_by_type('access_analysis.mitigation.activated')
    assert len(events) == 1
    assert events[0].payload['mitigation_id'] == str(mid)


@pytest.mark.asyncio
async def test_proposed_to_revoked_with_reason_emits_revoked(session_factory) -> None:
    """proposed → revoked with reason: status updated, reason persisted; one revoked event."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        mid = await _create_mitigation(svc, rule, ctrl, subject_id, owner_id)
        capturing.clear()

        result = await svc.patch_status(
            mid,
            MitigationStatusPatch(status=MitigationStatus.revoked, reason='no longer needed'),
        )
        assert result.status == MitigationStatus.revoked
        assert result.reason == 'no longer needed'

    events = capturing.filter_by_type('access_analysis.mitigation.revoked')
    assert len(events) == 1
    assert events[0].payload['reason'] == 'no longer needed'


@pytest.mark.asyncio
async def test_active_to_revoked_with_reason_emits_revoked(session_factory) -> None:
    """active → revoked with reason: status updated; one revoked event."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        mid = await _create_mitigation(svc, rule, ctrl, subject_id, owner_id, MitigationStatus.active)
        capturing.clear()

        result = await svc.patch_status(
            mid,
            MitigationStatusPatch(status=MitigationStatus.revoked, reason='access removed'),
        )
        assert result.status == MitigationStatus.revoked

    events = capturing.filter_by_type('access_analysis.mitigation.revoked')
    assert len(events) == 1


@pytest.mark.asyncio
async def test_active_to_revoked_without_reason_raises(session_factory) -> None:
    """active → revoked without reason raises MitigationReasonRequiredError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        mid = await _create_mitigation(svc, rule, ctrl, subject_id, owner_id, MitigationStatus.active)
        capturing.clear()

        with pytest.raises(MitigationReasonRequiredError):
            await svc.patch_status(mid, MitigationStatusPatch(status=MitigationStatus.revoked))

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_proposed_to_revoked_without_reason_raises(session_factory) -> None:
    """proposed → revoked without reason raises MitigationReasonRequiredError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        mid = await _create_mitigation(svc, rule, ctrl, subject_id, owner_id)
        capturing.clear()

        with pytest.raises(MitigationReasonRequiredError):
            await svc.patch_status(mid, MitigationStatusPatch(status=MitigationStatus.revoked))

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_proposed_to_expired_raises_transition_error(session_factory) -> None:
    """proposed → expired (via PATCH) raises MitigationStatusTransitionError (expired is sweep-only)."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        mid = await _create_mitigation(svc, rule, ctrl, subject_id, owner_id)
        capturing.clear()

        with pytest.raises(MitigationStatusTransitionError):
            await svc.patch_status(mid, MitigationStatusPatch(status=MitigationStatus.expired))


@pytest.mark.asyncio
async def test_active_to_active_raises_transition_error(session_factory) -> None:
    """active → active (no-op) raises MitigationStatusTransitionError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        mid = await _create_mitigation(svc, rule, ctrl, subject_id, owner_id, MitigationStatus.active)
        capturing.clear()

        with pytest.raises(MitigationStatusTransitionError):
            await svc.patch_status(mid, MitigationStatusPatch(status=MitigationStatus.active))


@pytest.mark.asyncio
async def test_revoked_to_active_raises_transition_error(session_factory) -> None:
    """revoked → active raises MitigationStatusTransitionError (terminal state)."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        m = await _insert_mitigation_direct(session, rule, ctrl, subject_id, owner_id, MitigationStatus.revoked)
        capturing.clear()

        with pytest.raises(MitigationStatusTransitionError):
            await svc.patch_status(m.id, MitigationStatusPatch(status=MitigationStatus.active))


@pytest.mark.asyncio
async def test_expired_to_active_raises_transition_error(session_factory) -> None:
    """expired → active raises MitigationStatusTransitionError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        m = await _insert_mitigation_direct(session, rule, ctrl, subject_id, owner_id, MitigationStatus.expired)
        capturing.clear()

        with pytest.raises(MitigationStatusTransitionError):
            await svc.patch_status(m.id, MitigationStatusPatch(status=MitigationStatus.active))


@pytest.mark.asyncio
async def test_patch_missing_id_raises_not_found(session_factory) -> None:
    """patch_status on missing id raises MitigationNotFoundError; no events."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        svc = _make_service(session, capturing)

        with pytest.raises(MitigationNotFoundError):
            await svc.patch_status(99999, MitigationStatusPatch(status=MitigationStatus.active))

    assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_activate_then_create_second_proposed_raises_duplicate(session_factory) -> None:
    """After activating, a second proposed for same (rule, subject, scope) → MitigationDuplicateActiveError."""
    capturing = CapturingEventService()
    async with session_factory() as session:
        rule = await _insert_rule(session)
        ctrl = await _insert_control(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        svc = _make_service(session, capturing)

        mid = await _create_mitigation(svc, rule, ctrl, subject_id, owner_id)
        await svc.patch_status(mid, MitigationStatusPatch(status=MitigationStatus.active))
        capturing.clear()

        from src.inventory.assessment.mitigations.schemas import MitigationCreate

        payload = MitigationCreate(
            rule_id=rule.id,
            control_id=ctrl.id,
            subject_id=subject_id,
            owner_id=owner_id,
            valid_from=_now(),
            status=MitigationStatus.proposed,
        )
        with pytest.raises(MitigationDuplicateActiveError):
            await svc.create(payload)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Event emission tests for FeedbackService."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import uuid

import pytest
from src.capabilities.access_analysis.feedbacks.exceptions import (
    FeedbackFindingNotFoundError,
    FeedbackRuleNotFoundError,
    FeedbackTargetMissingError,
)
from src.capabilities.access_analysis.feedbacks.models import FeedbackKind
from src.capabilities.access_analysis.feedbacks.schemas import FeedbackCreate
from src.capabilities.access_analysis.feedbacks.service import FeedbackService
from src.capabilities.access_analysis.findings.models import Finding, FindingKind, FindingStatus
from src.capabilities.access_analysis.scan_runs.models import ScanRun, ScanRunTrigger
from src.capabilities.access_analysis.sod_rules.models import SodRule, SodRuleScope, SodSeverity
from src.platform.events.schemas import EventParticipantKind
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(session, capturing: CapturingEventService) -> FeedbackService:
    event_service = EventService(sink=capturing)
    return FeedbackService(session, event_service)


async def _insert_rule(session) -> SodRule:
    rule = SodRule(
        code=f'EVT-FB-{uuid.uuid4().hex[:6]}',
        name='Event Feedback Rule',
        severity=SodSeverity.high,
        scope_mode=SodRuleScope.global_,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def _insert_subject(session) -> uuid.UUID:
    from src.inventory.nhi.repository import create_nhi
    from src.inventory.subjects.models import SubjectKind, SubjectNHIKind, SubjectNHIStatus
    from src.inventory.subjects.repository import create_subject

    nhi = await create_nhi(
        session,
        external_id=f'evt-fb-nhi-{uuid.uuid4().hex[:8]}',
        name='Event Feedback NHI',
        kind='service_account',
    )
    subject = await create_subject(
        session,
        external_id=f'evt-fb-subj-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status=SubjectNHIStatus.active,
    )
    return subject.id


async def _insert_finding(session, rule: SodRule, subject_id: uuid.UUID) -> Finding:
    run = ScanRun(triggered_by=ScanRunTrigger.manual)
    session.add(run)
    await session.flush()

    h = hashlib.sha256(f'{rule.id}:{subject_id}:{uuid.uuid4().hex}'.encode()).hexdigest()[:64]
    finding = Finding(
        scan_run_id=run.id,
        kind=FindingKind.sod,
        subject_id=subject_id,
        account_id=None,
        rule_id=rule.id,
        scope_key_id=None,
        scope_value=None,
        severity=SodSeverity.high,
        status=FindingStatus.open,
        matched_capability_grant_ids=[],
        matched_effective_grant_ids=[],
        matched_access_fact_ids=[],
        evidence_hash=h,
        evaluated_at=datetime.now(UTC),
    )
    session.add(finding)
    await session.flush()
    await session.refresh(finding)
    return finding


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_feedback_emits_exactly_one_event(session_factory) -> None:
    """create_feedback emits exactly one feedback.posted event."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            rule_id=rule.id,
            kind=FeedbackKind.needs_rule_fix,
            message='Test event emission',
            created_by='bob@example.com',
        )
        await svc.create_feedback(payload)

        assert len(capturing.emitted) == 1


@pytest.mark.asyncio
async def test_create_feedback_event_envelope_shape(session_factory) -> None:
    """Emitted event has correct event_type, component, level, and actor."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        subject_id = await _insert_subject(session)
        finding = await _insert_finding(session, rule, subject_id)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            finding_id=finding.id,
            kind=FeedbackKind.accepted_risk,
            message='Accepted risk for this finding',
            created_by='carol@example.com',
        )
        result = await svc.create_feedback(payload, correlation_id='test-corr-id')
        event = capturing.emitted[0]

        assert event.event_type == 'access_analysis.feedback.posted'
        assert event.actor_kind == EventParticipantKind.USER
        assert event.actor_id == 'carol@example.com'
        assert event.target_kind == EventParticipantKind.SYSTEM
        assert event.target_id == str(result.id)
        assert event.correlation_id == 'test-corr-id'
        assert event.causation_id is None


@pytest.mark.asyncio
async def test_create_feedback_event_payload_fields(session_factory) -> None:
    """Event payload contains feedback_id, finding_id, kind, author, note_excerpt, created_at."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        subject_id = await _insert_subject(session)
        finding = await _insert_finding(session, rule, subject_id)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            finding_id=finding.id,
            kind=FeedbackKind.false_positive,
            message='This is a false positive finding',
            created_by='dave@example.com',
        )
        result = await svc.create_feedback(payload)
        event = capturing.emitted[0]
        p = event.payload

        assert p['feedback_id'] == str(result.id)
        assert p['finding_id'] == str(finding.id)
        assert p['kind'] == FeedbackKind.false_positive
        assert p['author'] == 'dave@example.com'
        assert 'note_excerpt' in p
        assert 'created_at' in p


@pytest.mark.asyncio
async def test_create_feedback_note_excerpt_truncated_at_200(session_factory) -> None:
    """note_excerpt is exactly the first 200 characters when message is longer."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        long_message = 'A' * 300
        payload = FeedbackCreate(
            rule_id=rule.id,
            kind=FeedbackKind.needs_rule_fix,
            message=long_message,
        )
        await svc.create_feedback(payload)
        event = capturing.emitted[0]

        assert event.payload['note_excerpt'] == 'A' * 200


@pytest.mark.asyncio
async def test_create_feedback_note_excerpt_short_message(session_factory) -> None:
    """note_excerpt is the full message when message length <= 200."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        short_message = 'Short note'
        payload = FeedbackCreate(
            rule_id=rule.id,
            kind=FeedbackKind.needs_rule_fix,
            message=short_message,
        )
        await svc.create_feedback(payload)
        event = capturing.emitted[0]

        assert event.payload['note_excerpt'] == short_message


@pytest.mark.asyncio
async def test_create_feedback_actor_is_capability_when_no_created_by(session_factory) -> None:
    """When created_by is None, actor_kind = CAPABILITY and actor_id = component name."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            rule_id=rule.id,
            kind=FeedbackKind.needs_rule_fix,
            message='system generated',
        )
        await svc.create_feedback(payload)
        event = capturing.emitted[0]

        assert event.actor_kind == EventParticipantKind.CAPABILITY
        assert event.actor_id == 'access_analysis.feedbacks'


@pytest.mark.asyncio
async def test_no_event_emitted_on_target_missing_error(session_factory) -> None:
    """FeedbackTargetMissingError → zero events emitted."""
    async with session_factory() as session:
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            kind=FeedbackKind.accepted_risk,
            message='no target',
        )
        with pytest.raises(FeedbackTargetMissingError):
            await svc.create_feedback(payload)

        assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_no_event_emitted_on_rule_not_found_error(session_factory) -> None:
    """FeedbackRuleNotFoundError → zero events emitted."""
    async with session_factory() as session:
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            rule_id=999_999_999,
            kind=FeedbackKind.needs_rule_fix,
            message='ghost rule',
        )
        with pytest.raises(FeedbackRuleNotFoundError):
            await svc.create_feedback(payload)

        assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_no_event_emitted_on_finding_not_found_error(session_factory) -> None:
    """FeedbackFindingNotFoundError → zero events emitted."""
    async with session_factory() as session:
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            finding_id=999_999_999,
            kind=FeedbackKind.false_positive,
            message='ghost finding',
        )
        with pytest.raises(FeedbackFindingNotFoundError):
            await svc.create_feedback(payload)

        assert len(capturing.emitted) == 0

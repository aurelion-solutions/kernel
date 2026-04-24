# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer tests for FeedbackService."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import uuid

import pytest
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
from src.capabilities.access_analysis.feedbacks.exceptions import (
    FeedbackCapabilityMappingNotFoundError,
    FeedbackFindingNotFoundError,
    FeedbackRuleNotFoundError,
    FeedbackSubjectNotFoundError,
    FeedbackTargetMissingError,
)
from src.capabilities.access_analysis.feedbacks.models import FeedbackKind
from src.capabilities.access_analysis.feedbacks.schemas import FeedbackCreate, FeedbackRead
from src.capabilities.access_analysis.feedbacks.service import FeedbackService
from src.capabilities.access_analysis.findings.models import Finding, FindingKind, FindingStatus
from src.capabilities.access_analysis.scan_runs.models import ScanRun, ScanRunTrigger
from src.capabilities.access_analysis.sod_rules.models import SodRule, SodRuleScope, SodSeverity
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
        code=f'SVC-FB-{uuid.uuid4().hex[:6]}',
        name='Feedback Service Rule',
        severity=SodSeverity.high,
        scope_mode=SodRuleScope.global_,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def _insert_capability_mapping(session) -> CapabilityMapping:
    cap = Capability(
        slug=f'svc-fb-cap-{uuid.uuid4().hex[:6]}',
        name='Service Feedback Capability',
    )
    session.add(cap)
    await session.flush()

    scope_key = CapabilityScopeKey(
        code=f'SVC-FB-SK-{uuid.uuid4().hex[:6]}',
        name='Service Feedback Scope Key',
    )
    session.add(scope_key)
    await session.flush()

    mapping = CapabilityMapping(
        capability_id=cap.id,
        scope_key_id=scope_key.id,
        resource_kind='test_resource',
        scope_value_source={'kind': 'constant', 'value': 'v'},
    )
    session.add(mapping)
    await session.flush()
    await session.refresh(mapping)
    return mapping


async def _insert_subject(session) -> uuid.UUID:
    from src.inventory.nhi.repository import create_nhi
    from src.inventory.subjects.models import SubjectKind, SubjectNHIKind, SubjectNHIStatus
    from src.inventory.subjects.repository import create_subject

    nhi = await create_nhi(
        session,
        external_id=f'svc-fb-nhi-{uuid.uuid4().hex[:8]}',
        name='Service Feedback NHI',
        kind='service_account',
    )
    subject = await create_subject(
        session,
        external_id=f'svc-fb-subj-{uuid.uuid4().hex[:8]}',
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
# create_feedback happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_feedback_with_finding_id_only(session_factory) -> None:
    """Happy path: feedback with finding_id only → persisted, returns FeedbackRead."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        subject_id = await _insert_subject(session)
        finding = await _insert_finding(session, rule, subject_id)

        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            finding_id=finding.id,
            kind=FeedbackKind.false_positive,
            message='This looks like a false positive',
            created_by='alice@example.com',
        )
        result = await svc.create_feedback(payload)

        assert isinstance(result, FeedbackRead)
        assert result.id is not None
        assert result.id > 0
        assert result.finding_id == finding.id
        assert result.rule_id is None
        assert result.capability_mapping_id is None
        assert result.kind == FeedbackKind.false_positive
        assert result.created_at is not None


@pytest.mark.asyncio
async def test_create_feedback_with_capability_mapping_id_only(session_factory) -> None:
    """Happy path: feedback with capability_mapping_id only → persisted."""
    async with session_factory() as session:
        mapping = await _insert_capability_mapping(session)

        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            capability_mapping_id=mapping.id,
            kind=FeedbackKind.needs_mapping_fix,
            message='Mapping needs update',
        )
        result = await svc.create_feedback(payload)

        assert isinstance(result, FeedbackRead)
        assert result.id is not None
        assert result.capability_mapping_id == mapping.id
        assert result.finding_id is None
        assert result.rule_id is None


@pytest.mark.asyncio
async def test_create_feedback_with_rule_id_only(session_factory) -> None:
    """Happy path: feedback with rule_id only → persisted."""
    async with session_factory() as session:
        rule = await _insert_rule(session)

        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            rule_id=rule.id,
            kind=FeedbackKind.needs_rule_fix,
            message='Rule is too broad',
        )
        result = await svc.create_feedback(payload)

        assert isinstance(result, FeedbackRead)
        assert result.id is not None
        assert result.rule_id == rule.id
        assert result.finding_id is None
        assert result.capability_mapping_id is None


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_feedback_no_targets_raises(session_factory) -> None:
    """All three target FKs unset → FeedbackTargetMissingError."""
    async with session_factory() as session:
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            kind=FeedbackKind.accepted_risk,
            message='I accept this risk',
        )
        with pytest.raises(FeedbackTargetMissingError):
            await svc.create_feedback(payload)

        assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_feedback_nonexistent_rule_raises(session_factory) -> None:
    """Non-existent rule_id → FeedbackRuleNotFoundError."""
    async with session_factory() as session:
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            rule_id=999_999_999,
            kind=FeedbackKind.needs_rule_fix,
            message='does not exist',
        )
        with pytest.raises(FeedbackRuleNotFoundError):
            await svc.create_feedback(payload)

        assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_feedback_nonexistent_mapping_raises(session_factory) -> None:
    """Non-existent capability_mapping_id → FeedbackCapabilityMappingNotFoundError."""
    async with session_factory() as session:
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            capability_mapping_id=999_999_999,
            kind=FeedbackKind.needs_mapping_fix,
            message='missing mapping',
        )
        with pytest.raises(FeedbackCapabilityMappingNotFoundError):
            await svc.create_feedback(payload)

        assert len(capturing.emitted) == 0


@pytest.mark.asyncio
async def test_create_feedback_nonexistent_finding_raises(session_factory) -> None:
    """Non-existent finding_id → FeedbackFindingNotFoundError."""
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


@pytest.mark.asyncio
async def test_create_feedback_nonexistent_subject_raises(session_factory) -> None:
    """Non-existent subject_id when provided → FeedbackSubjectNotFoundError."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        payload = FeedbackCreate(
            rule_id=rule.id,
            subject_id=uuid.uuid4(),
            kind=FeedbackKind.accepted_risk,
            message='test',
        )
        with pytest.raises(FeedbackSubjectNotFoundError):
            await svc.create_feedback(payload)

        assert len(capturing.emitted) == 0


# ---------------------------------------------------------------------------
# list_feedbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_feedbacks_filter_by_kind(session_factory) -> None:
    """list_feedbacks(kind=...) returns only feedbacks of that kind."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        await svc.create_feedback(FeedbackCreate(rule_id=rule.id, kind=FeedbackKind.false_positive, message='fp'))
        await svc.create_feedback(FeedbackCreate(rule_id=rule.id, kind=FeedbackKind.accepted_risk, message='ar'))

        results = await svc.list_feedbacks(kind=FeedbackKind.false_positive)
        assert all(r.kind == FeedbackKind.false_positive for r in results)
        assert len(results) >= 1


@pytest.mark.asyncio
async def test_list_feedbacks_filter_by_rule_id(session_factory) -> None:
    """list_feedbacks(rule_id=...) returns only feedbacks for that rule."""
    async with session_factory() as session:
        rule1 = await _insert_rule(session)
        rule2 = await _insert_rule(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        await svc.create_feedback(FeedbackCreate(rule_id=rule1.id, kind=FeedbackKind.needs_rule_fix, message='r1'))
        await svc.create_feedback(FeedbackCreate(rule_id=rule2.id, kind=FeedbackKind.needs_rule_fix, message='r2'))

        results = await svc.list_feedbacks(rule_id=rule1.id)
        assert all(r.rule_id == rule1.id for r in results)
        assert len(results) >= 1


@pytest.mark.asyncio
async def test_list_feedbacks_filter_by_capability_mapping_id(session_factory) -> None:
    """list_feedbacks(capability_mapping_id=...) returns only matching feedbacks."""
    async with session_factory() as session:
        mapping = await _insert_capability_mapping(session)
        rule = await _insert_rule(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        await svc.create_feedback(
            FeedbackCreate(
                capability_mapping_id=mapping.id,
                kind=FeedbackKind.needs_mapping_fix,
                message='fix mapping',
            )
        )
        await svc.create_feedback(FeedbackCreate(rule_id=rule.id, kind=FeedbackKind.needs_rule_fix, message='other'))

        results = await svc.list_feedbacks(capability_mapping_id=mapping.id)
        assert all(r.capability_mapping_id == mapping.id for r in results)
        assert len(results) >= 1


@pytest.mark.asyncio
async def test_list_feedbacks_filter_by_finding_id(session_factory) -> None:
    """list_feedbacks(finding_id=...) returns only matching feedbacks."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        subject_id = await _insert_subject(session)
        finding = await _insert_finding(session, rule, subject_id)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        await svc.create_feedback(
            FeedbackCreate(
                finding_id=finding.id,
                kind=FeedbackKind.false_positive,
                message='fp on finding',
            )
        )
        await svc.create_feedback(FeedbackCreate(rule_id=rule.id, kind=FeedbackKind.needs_rule_fix, message='other'))

        results = await svc.list_feedbacks(finding_id=finding.id)
        assert all(r.finding_id == finding.id for r in results)
        assert len(results) >= 1


@pytest.mark.asyncio
async def test_list_feedbacks_filter_by_subject_id(session_factory) -> None:
    """list_feedbacks(subject_id=...) returns only matching feedbacks."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        subject_id = await _insert_subject(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        await svc.create_feedback(
            FeedbackCreate(
                rule_id=rule.id,
                subject_id=subject_id,
                kind=FeedbackKind.accepted_risk,
                message='for this subject',
            )
        )
        await svc.create_feedback(FeedbackCreate(rule_id=rule.id, kind=FeedbackKind.needs_rule_fix, message='other'))

        results = await svc.list_feedbacks(subject_id=subject_id)
        assert all(r.subject_id == subject_id for r in results)
        assert len(results) >= 1


@pytest.mark.asyncio
async def test_list_feedbacks_ordered_by_created_at_desc(session_factory) -> None:
    """list_feedbacks returns rows ordered by created_at DESC."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        for i in range(3):
            await svc.create_feedback(
                FeedbackCreate(
                    rule_id=rule.id,
                    kind=FeedbackKind.needs_rule_fix,
                    message=f'feedback {i}',
                )
            )

        results = await svc.list_feedbacks(rule_id=rule.id)
        assert len(results) >= 3
        for i in range(len(results) - 1):
            assert results[i].created_at >= results[i + 1].created_at


@pytest.mark.asyncio
async def test_list_feedbacks_combined_filters(session_factory) -> None:
    """list_feedbacks with multiple filters applied simultaneously."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        subject_id = await _insert_subject(session)
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)

        # This feedback matches both filters
        await svc.create_feedback(
            FeedbackCreate(
                rule_id=rule.id,
                subject_id=subject_id,
                kind=FeedbackKind.accepted_risk,
                message='both match',
            )
        )
        # This feedback matches only rule_id
        await svc.create_feedback(
            FeedbackCreate(
                rule_id=rule.id,
                kind=FeedbackKind.accepted_risk,
                message='only rule',
            )
        )

        results = await svc.list_feedbacks(
            rule_id=rule.id,
            kind=FeedbackKind.accepted_risk,
            subject_id=subject_id,
        )
        assert all(r.subject_id == subject_id for r in results)
        assert all(r.rule_id == rule.id for r in results)


# ---------------------------------------------------------------------------
# get_feedback_by_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_feedback_by_id_missing_returns_none(session_factory) -> None:
    """get_feedback_by_id returns None for a missing id (route maps to 404)."""
    async with session_factory() as session:
        capturing = CapturingEventService()
        svc = _make_service(session, capturing)
        result = await svc.get_feedback_by_id(999_999_999)
        assert result is None

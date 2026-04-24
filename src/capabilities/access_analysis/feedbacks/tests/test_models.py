# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Model tests for the Feedback ORM."""

from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
from src.capabilities.access_analysis.feedbacks.models import Feedback, FeedbackKind
from src.capabilities.access_analysis.findings.models import Finding, FindingKind, FindingStatus
from src.capabilities.access_analysis.scan_runs.models import ScanRun, ScanRunTrigger
from src.capabilities.access_analysis.sod_rules.models import SodRule, SodRuleScope, SodSeverity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_rule(session) -> SodRule:
    rule = SodRule(
        code=f'RULE-FB-{uuid.uuid4().hex[:6]}',
        name='Feedback Test Rule',
        severity=SodSeverity.high,
        scope_mode=SodRuleScope.global_,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def _insert_capability_mapping(session) -> CapabilityMapping:
    cap = Capability(
        slug=f'cap-fb-{uuid.uuid4().hex[:6]}',
        name='Feedback Test Capability',
    )
    session.add(cap)
    await session.flush()

    scope_key = CapabilityScopeKey(
        code=f'SC-FB-{uuid.uuid4().hex[:6]}',
        name='Feedback Scope Key',
    )
    session.add(scope_key)
    await session.flush()

    mapping = CapabilityMapping(
        capability_id=cap.id,
        scope_key_id=scope_key.id,
        resource_kind='test_resource',
        scope_value_source={'kind': 'constant', 'value': 'test'},
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
        external_id=f'fb-nhi-{uuid.uuid4().hex[:8]}',
        name='Feedback NHI',
        kind='service_account',
    )
    subject = await create_subject(
        session,
        external_id=f'fb-subj-{uuid.uuid4().hex[:8]}',
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
async def test_create_feedback_with_all_fks(session_factory) -> None:
    """Inserting a Feedback with all nullable FK columns set persists all columns."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        mapping = await _insert_capability_mapping(session)
        subject_id = await _insert_subject(session)
        finding = await _insert_finding(session, rule, subject_id)

        fb = Feedback(
            rule_id=rule.id,
            capability_mapping_id=mapping.id,
            finding_id=finding.id,
            subject_id=subject_id,
            kind=FeedbackKind.accepted_risk,
            message='Test message',
            payload={'reason': 'ok'},
            created_by='alice@example.com',
        )
        session.add(fb)
        await session.flush()
        await session.refresh(fb)

        assert fb.id is not None
        assert fb.rule_id == rule.id
        assert fb.capability_mapping_id == mapping.id
        assert fb.finding_id == finding.id
        assert fb.subject_id == subject_id
        assert fb.kind == FeedbackKind.accepted_risk
        assert fb.message == 'Test message'
        assert fb.payload == {'reason': 'ok'}
        assert fb.created_at is not None
        assert fb.created_by == 'alice@example.com'


@pytest.mark.asyncio
async def test_all_feedback_kind_values_round_trip(session_factory) -> None:
    """Every FeedbackKind value is accepted by insert."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        for kind in FeedbackKind:
            fb = Feedback(
                rule_id=rule.id,
                kind=kind,
                message=f'Testing kind {kind}',
            )
            session.add(fb)
            await session.flush()
            assert fb.kind == kind


@pytest.mark.asyncio
async def test_check_constraint_rejects_all_target_fks_null(session_factory) -> None:
    """INSERT with all three target FKs NULL → IntegrityError (ck_feedbacks_target_required)."""
    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text("INSERT INTO feedbacks (kind, message) VALUES ('accepted_risk', 'no target')")
            )
            await session.flush()


@pytest.mark.asyncio
async def test_fk_restrict_prevents_deleting_rule_with_feedback(session_factory) -> None:
    """Deleting a SodRule that has a Feedback → IntegrityError (FK RESTRICT)."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        fb = Feedback(
            rule_id=rule.id,
            kind=FeedbackKind.needs_rule_fix,
            message='rule has issues',
        )
        session.add(fb)
        await session.flush()
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text('DELETE FROM sod_rules WHERE id = :id'),
                {'id': rule.id},
            )
            await session.flush()


@pytest.mark.asyncio
async def test_fk_restrict_prevents_deleting_mapping_with_feedback(session_factory) -> None:
    """Deleting a CapabilityMapping that has a Feedback → IntegrityError (FK RESTRICT)."""
    async with session_factory() as session:
        mapping = await _insert_capability_mapping(session)
        fb = Feedback(
            capability_mapping_id=mapping.id,
            kind=FeedbackKind.needs_mapping_fix,
            message='mapping issue',
        )
        session.add(fb)
        await session.flush()
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text('DELETE FROM capability_mappings WHERE id = :id'),
                {'id': mapping.id},
            )
            await session.flush()


@pytest.mark.asyncio
async def test_fk_restrict_prevents_deleting_finding_with_feedback(session_factory) -> None:
    """Deleting a Finding that has a Feedback → IntegrityError (FK RESTRICT)."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        subject_id = await _insert_subject(session)
        finding = await _insert_finding(session, rule, subject_id)
        fb = Feedback(
            finding_id=finding.id,
            kind=FeedbackKind.false_positive,
            message='false alarm',
        )
        session.add(fb)
        await session.flush()
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text('DELETE FROM findings WHERE id = :id'),
                {'id': finding.id},
            )
            await session.flush()


@pytest.mark.asyncio
async def test_fk_restrict_prevents_deleting_subject_with_feedback(session_factory) -> None:
    """Deleting a Subject that has a Feedback → IntegrityError (FK RESTRICT)."""
    async with session_factory() as session:
        rule = await _insert_rule(session)
        subject_id = await _insert_subject(session)
        fb = Feedback(
            rule_id=rule.id,
            subject_id=subject_id,
            kind=FeedbackKind.accepted_risk,
            message='accepted',
        )
        session.add(fb)
        await session.flush()
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text('DELETE FROM subjects WHERE id = :id'),
                {'id': str(subject_id)},
            )
            await session.flush()

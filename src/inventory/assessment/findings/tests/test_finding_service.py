# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer tests for FindingService.

Findings are inserted directly via session.add() — no FindingCreate API exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from src.inventory.assessment.findings.exceptions import (
    FindingMissingReasonError,
    FindingNotFoundError,
    FindingStatusTransitionError,
)
from src.inventory.assessment.findings.models import Finding, FindingKind, FindingStatus
from src.inventory.assessment.findings.schemas import FindingStatusPatch
from src.inventory.assessment.findings.service import FindingService
from src.inventory.assessment.mitigation_controls.models import MitigationControl, MitigationControlType
from src.inventory.assessment.mitigations.models import Mitigation, MitigationStatus
from src.inventory.assessment.scan_runs.models import ScanRun, ScanRunTrigger
from src.inventory.policy.sod_rules.models import SodRule, SodRuleScope, SodSeverity
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(session) -> FindingService:
    return FindingService(session, NoOpLogService())


async def _insert_scan_run(session) -> ScanRun:
    """Insert a minimal ScanRun and return the ORM object."""
    run = ScanRun(triggered_by=ScanRunTrigger.manual)
    session.add(run)
    await session.flush()
    await session.refresh(run)
    return run


async def _insert_sod_rule(session) -> SodRule:
    """Insert a minimal SodRule and return the ORM object."""
    code = f'SVC-TST-{uuid.uuid4().hex[:6]}'
    rule = SodRule(
        code=code,
        name='Test Rule',
        severity=SodSeverity.high,
        scope_mode=SodRuleScope.global_,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def _insert_subject(session) -> uuid.UUID:
    """Insert a Subject (via NHI principal) and return its id."""
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


async def _insert_active_mitigation(session, rule_id: int, subject_id: uuid.UUID, owner_id: uuid.UUID) -> int:
    """Insert an active Mitigation and return its id."""
    from datetime import UTC, timedelta

    ctrl = MitigationControl(
        code=f'CTRL-{uuid.uuid4().hex[:6]}',
        name='Test Ctrl',
        type=MitigationControlType.attestation,
        is_active=True,
    )
    session.add(ctrl)
    await session.flush()
    mit = Mitigation(
        rule_id=rule_id,
        control_id=ctrl.id,
        subject_id=subject_id,
        scope_key_id=None,
        scope_value=None,
        status=MitigationStatus.active,
        valid_from=datetime.now(tz=UTC) - timedelta(days=1),
        valid_until=None,
        owner_id=owner_id,
    )
    session.add(mit)
    await session.flush()
    return mit.id


async def _insert_account(session) -> uuid.UUID:
    """Insert a minimal Application + Account and return the account id."""
    from src.inventory.accounts.models import Account
    from src.platform.applications.models import Application

    app = Application(
        name=f'App-{uuid.uuid4().hex[:8]}',
        code=f'APP-{uuid.uuid4().hex[:8]}',
    )
    session.add(app)
    await session.flush()

    account = Account(
        application_id=app.id,
        username=f'user-{uuid.uuid4().hex[:8]}',
    )
    session.add(account)
    await session.flush()
    await session.refresh(account)
    return account.id


def _sod_finding(scan_run: ScanRun, rule: SodRule, subject_id: uuid.UUID, hash_suffix: str = '0') -> Finding:
    return Finding(
        scan_run_id=scan_run.id,
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
        evidence_hash=hash_suffix * 64,
        evaluated_at=datetime.now(tz=UTC),
    )


def _orphan_finding(scan_run: ScanRun, account_id: uuid.UUID, hash_suffix: str = '1') -> Finding:
    return Finding(
        scan_run_id=scan_run.id,
        kind=FindingKind.orphan_access,
        subject_id=None,
        account_id=account_id,
        rule_id=None,
        scope_key_id=None,
        scope_value=None,
        severity=SodSeverity.medium,
        status=FindingStatus.open,
        matched_capability_grant_ids=[],
        matched_effective_grant_ids=[],
        matched_access_fact_ids=[],
        evidence_hash=hash_suffix * 64,
        evaluated_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# Model / DB smoke tests (covers FK + enum + JSONB + CHECK)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_sod_and_orphan_findings_ok(session_factory) -> None:
    """Insert a SodRule + ScanRun + two Finding kinds; query both back."""
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        rule = await _insert_sod_rule(session)
        subject_id = await _insert_subject(session)
        account_id = await _insert_account(session)

        f_sod = _sod_finding(run, rule, subject_id, '0')
        f_orphan = _orphan_finding(run, account_id, '1')
        session.add(f_sod)
        session.add(f_orphan)
        await session.flush()

        result = await session.execute(sa.select(Finding).where(Finding.scan_run_id == run.id))
        findings = list(result.scalars().all())
        await session.commit()

    assert len(findings) == 2


@pytest.mark.asyncio
async def test_sod_finding_without_rule_id_rejected(session_factory) -> None:
    """kind=sod with rule_id=None must be rejected by DB CHECK."""
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        subject_id = await _insert_subject(session)

        bad = Finding(
            scan_run_id=run.id,
            kind=FindingKind.sod,
            subject_id=subject_id,
            account_id=None,
            rule_id=None,  # violates ck_findings_rule_id_for_sod
            scope_key_id=None,
            scope_value=None,
            severity=SodSeverity.high,
            status=FindingStatus.open,
            matched_capability_grant_ids=[],
            matched_effective_grant_ids=[],
            matched_access_fact_ids=[],
            evidence_hash='a' * 64,
            evaluated_at=datetime.now(tz=UTC),
        )
        session.add(bad)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_orphan_finding_with_subject_id_rejected(session_factory) -> None:
    """kind=orphan_access with subject_id set must be rejected by DB CHECK."""
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        subject_id = await _insert_subject(session)
        account_id = await _insert_account(session)

        bad = Finding(
            scan_run_id=run.id,
            kind=FindingKind.orphan_access,
            subject_id=subject_id,  # violates ck_findings_orphan_no_subject
            account_id=account_id,
            rule_id=None,
            scope_key_id=None,
            scope_value=None,
            severity=SodSeverity.medium,
            status=FindingStatus.open,
            matched_capability_grant_ids=[],
            matched_effective_grant_ids=[],
            matched_access_fact_ids=[],
            evidence_hash='b' * 64,
            evaluated_at=datetime.now(tz=UTC),
        )
        session.add(bad)
        with pytest.raises(IntegrityError):
            await session.flush()


# ---------------------------------------------------------------------------
# patch_status tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_status_open_to_acknowledged_sets_status_changed_at(session_factory) -> None:
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        rule = await _insert_sod_rule(session)
        subject_id = await _insert_subject(session)
        f = _sod_finding(run, rule, subject_id, '2')
        session.add(f)
        await session.flush()
        await session.refresh(f)
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        result = await svc.patch_status(finding_id, FindingStatusPatch(status=FindingStatus.acknowledged))
        await session.commit()

    assert result.status == FindingStatus.acknowledged
    assert result.status_changed_at is not None


@pytest.mark.asyncio
async def test_patch_status_open_to_mitigated(session_factory) -> None:
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        rule = await _insert_sod_rule(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        mit_id = await _insert_active_mitigation(session, rule.id, subject_id, owner_id)
        f = _sod_finding(run, rule, subject_id, '3')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        result = await svc.patch_status(
            finding_id,
            FindingStatusPatch(status=FindingStatus.mitigated, active_mitigation_id=mit_id),
        )
        await session.commit()

    assert result.status == FindingStatus.mitigated
    assert result.active_mitigation_id == mit_id


@pytest.mark.asyncio
async def test_patch_status_acknowledged_to_mitigated(session_factory) -> None:
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        rule = await _insert_sod_rule(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        mit_id = await _insert_active_mitigation(session, rule.id, subject_id, owner_id)
        f = _sod_finding(run, rule, subject_id, '4')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        await svc.patch_status(finding_id, FindingStatusPatch(status=FindingStatus.acknowledged))
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        result = await svc.patch_status(
            finding_id,
            FindingStatusPatch(status=FindingStatus.mitigated, active_mitigation_id=mit_id),
        )
        await session.commit()

    assert result.status == FindingStatus.mitigated
    assert result.active_mitigation_id == mit_id


@pytest.mark.asyncio
async def test_patch_status_open_to_resolved_without_reason_raises(session_factory) -> None:
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        rule = await _insert_sod_rule(session)
        subject_id = await _insert_subject(session)
        f = _sod_finding(run, rule, subject_id, '5')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingMissingReasonError):
            await svc.patch_status(finding_id, FindingStatusPatch(status=FindingStatus.resolved))


@pytest.mark.asyncio
async def test_patch_status_open_to_resolved_with_reason(session_factory) -> None:
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        rule = await _insert_sod_rule(session)
        subject_id = await _insert_subject(session)
        f = _sod_finding(run, rule, subject_id, '6')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        result = await svc.patch_status(
            finding_id,
            FindingStatusPatch(status=FindingStatus.resolved, status_reason='Operator override'),
        )
        await session.commit()

    assert result.status == FindingStatus.resolved
    assert result.status_reason == 'Operator override'


@pytest.mark.asyncio
async def test_patch_status_mitigated_to_open_raises(session_factory) -> None:
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        rule = await _insert_sod_rule(session)
        subject_id = await _insert_subject(session)
        owner_id = await _insert_subject(session)
        mit_id = await _insert_active_mitigation(session, rule.id, subject_id, owner_id)
        f = _sod_finding(run, rule, subject_id, '7')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        await svc.patch_status(
            finding_id,
            FindingStatusPatch(status=FindingStatus.mitigated, active_mitigation_id=mit_id),
        )
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingStatusTransitionError):
            await svc.patch_status(finding_id, FindingStatusPatch(status=FindingStatus.open))


@pytest.mark.asyncio
async def test_patch_status_resolved_to_any_raises(session_factory) -> None:
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        rule = await _insert_sod_rule(session)
        subject_id = await _insert_subject(session)
        f = _sod_finding(run, rule, subject_id, '8')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        await svc.patch_status(
            finding_id,
            FindingStatusPatch(status=FindingStatus.resolved, status_reason='Reason'),
        )
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingStatusTransitionError):
            await svc.patch_status(finding_id, FindingStatusPatch(status=FindingStatus.acknowledged))


@pytest.mark.asyncio
async def test_patch_status_open_to_open_raises(session_factory) -> None:
    async with session_factory() as session:
        run = await _insert_scan_run(session)
        rule = await _insert_sod_rule(session)
        subject_id = await _insert_subject(session)
        f = _sod_finding(run, rule, subject_id, '9')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingStatusTransitionError):
            await svc.patch_status(finding_id, FindingStatusPatch(status=FindingStatus.open))


# ---------------------------------------------------------------------------
# List / filter tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_with_multiple_filters(session_factory) -> None:
    async with session_factory() as session:
        run1 = await _insert_scan_run(session)
        run2 = await _insert_scan_run(session)
        rule = await _insert_sod_rule(session)
        subject_id = await _insert_subject(session)
        account_id = await _insert_account(session)

        f1 = _sod_finding(run1, rule, subject_id, 'a')
        f2 = _orphan_finding(run2, account_id, 'b')
        session.add(f1)
        session.add(f2)
        await session.flush()
        f1_id = f1.id
        f2_id = f2.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)

        # filter by scan_run_id
        by_run1 = await svc.list(scan_run_id=run1.id)
        assert any(f.id == f1_id for f in by_run1)
        assert not any(f.id == f2_id for f in by_run1)

        # filter by rule_id
        by_rule = await svc.list(rule_id=rule.id)
        assert any(f.id == f1_id for f in by_rule)

        # filter by severity
        by_severity = await svc.list(severity=SodSeverity.high)
        assert any(f.id == f1_id for f in by_severity)

        # filter by status
        by_status = await svc.list(status=FindingStatus.open)
        ids = [f.id for f in by_status]
        assert f1_id in ids
        assert f2_id in ids

        # filter by kind
        by_kind = await svc.list(kind=FindingKind.orphan_access)
        assert any(f.id == f2_id for f in by_kind)
        assert not any(f.id == f1_id for f in by_kind)

        # filter by subject_id
        by_subject = await svc.list(subject_id=subject_id)
        assert any(f.id == f1_id for f in by_subject)
        assert not any(f.id == f2_id for f in by_subject)


@pytest.mark.asyncio
async def test_get_missing_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingNotFoundError):
            await svc.get(999999)

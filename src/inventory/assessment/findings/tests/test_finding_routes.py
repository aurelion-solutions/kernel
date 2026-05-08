# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /findings routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from src.inventory.assessment.findings.models import Finding, FindingKind, FindingStatus
from src.inventory.assessment.mitigation_controls.models import MitigationControl, MitigationControlType
from src.inventory.assessment.mitigations.models import Mitigation, MitigationStatus
from src.inventory.assessment.scan_runs.models import ScanRun, ScanRunTrigger
from src.inventory.policy.sod_rules.models import SodRule, SodRuleScope, SodSeverity

_BASE = '/api/v0/findings'


async def _insert_scan_run(session_factory):
    """Insert a pending ScanRun and return its id."""
    async with session_factory() as session:
        run = ScanRun(triggered_by=ScanRunTrigger.manual)
        session.add(run)
        await session.flush()
        run_id = run.id
        await session.commit()
    return run_id


async def _insert_sod_rule(session_factory) -> int:
    """Insert a SodRule and return its id."""
    async with session_factory() as session:
        rule = SodRule(
            code=f'RT-{uuid.uuid4().hex[:8]}',
            name='Route Test Rule',
            severity=SodSeverity.high,
            scope_mode=SodRuleScope.global_,
        )
        session.add(rule)
        await session.flush()
        rule_id = rule.id
        await session.commit()
    return rule_id


async def _insert_subject(session_factory) -> uuid.UUID:
    """Insert a Subject (via NHI principal) and return its id."""
    from src.inventory.nhi.repository import create_nhi
    from src.inventory.subjects.models import SubjectKind, SubjectNHIKind, SubjectNHIStatus
    from src.inventory.subjects.repository import create_subject

    async with session_factory() as session:
        nhi = await create_nhi(
            session,
            external_id=f'rt-nhi-{uuid.uuid4().hex[:8]}',
            name='Route NHI',
            kind='service_account',
        )
        subject = await create_subject(
            session,
            external_id=f'rt-subj-{uuid.uuid4().hex[:8]}',
            kind=SubjectKind.nhi,
            nhi_kind=SubjectNHIKind.service_account,
            principal_nhi_id=nhi.id,
            status=SubjectNHIStatus.active,
        )
        subject_id = subject.id
        await session.commit()
    return subject_id


async def _insert_active_mitigation(session_factory, rule_id: int, subject_id: uuid.UUID) -> int:
    """Insert an active Mitigation (owner = subject_id) and return its id."""
    async with session_factory() as session:
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
            owner_id=subject_id,
        )
        session.add(mit)
        await session.flush()
        mit_id = mit.id
        await session.commit()
    return mit_id


async def _insert_finding(session_factory, scan_run_id: int, rule_id: int, subject_id: uuid.UUID, h: str) -> int:
    """Insert a sod Finding and return its id."""
    async with session_factory() as session:
        f = Finding(
            scan_run_id=scan_run_id,
            kind=FindingKind.sod,
            subject_id=subject_id,
            account_id=None,
            rule_id=rule_id,
            scope_key_id=None,
            scope_value=None,
            severity=SodSeverity.high,
            status=FindingStatus.open,
            matched_capability_grant_ids=[],
            matched_effective_grant_ids=[],
            matched_access_fact_ids=[],
            evidence_hash=h * 64,
            evaluated_at=datetime.now(tz=UTC),
        )
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()
    return finding_id


@pytest.fixture
async def test_finding(session_factory):
    """Create one finding for reuse in route tests."""
    run_id = await _insert_scan_run(session_factory)
    rule_id = await _insert_sod_rule(session_factory)
    subject_id = await _insert_subject(session_factory)
    finding_id = await _insert_finding(session_factory, run_id, rule_id, subject_id, 'f')
    return finding_id, run_id, rule_id, subject_id


@pytest.mark.asyncio
async def test_get_findings_with_filters(client, session_factory) -> None:
    run_id = await _insert_scan_run(session_factory)
    rule_id = await _insert_sod_rule(session_factory)
    subject_id = await _insert_subject(session_factory)

    f1_id = await _insert_finding(session_factory, run_id, rule_id, subject_id, 'c')
    run2_id = await _insert_scan_run(session_factory)
    rule2_id = await _insert_sod_rule(session_factory)
    f2_id = await _insert_finding(session_factory, run2_id, rule2_id, subject_id, 'd')

    resp = await client.get(f'{_BASE}?scan_run_id={run_id}')
    assert resp.status_code == 200
    ids = [f['id'] for f in resp.json()]
    assert f1_id in ids
    assert f2_id not in ids


@pytest.mark.asyncio
async def test_get_finding_by_id_missing_returns_404(client) -> None:
    resp = await client.get(f'{_BASE}/99999')
    assert resp.status_code == 404
    assert 'not found' in resp.json()['detail'].lower()


@pytest.mark.asyncio
async def test_patch_finding_status_open_to_acknowledged_returns_200(client, test_finding) -> None:
    finding_id, *_ = test_finding
    resp = await client.patch(f'{_BASE}/{finding_id}/status', json={'status': 'acknowledged'})
    assert resp.status_code == 200
    assert resp.json()['status'] == 'acknowledged'


@pytest.mark.asyncio
async def test_patch_finding_status_mitigated_to_open_returns_422(client, session_factory) -> None:
    run_id = await _insert_scan_run(session_factory)
    rule_id = await _insert_sod_rule(session_factory)
    subject_id = await _insert_subject(session_factory)
    finding_id = await _insert_finding(session_factory, run_id, rule_id, subject_id, 'e')
    mit_id = await _insert_active_mitigation(session_factory, rule_id, subject_id)

    await client.patch(
        f'{_BASE}/{finding_id}/status',
        json={'status': 'mitigated', 'active_mitigation_id': mit_id},
    )

    resp = await client.patch(f'{_BASE}/{finding_id}/status', json={'status': 'open'})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_finding_status_open_to_resolved_without_reason_returns_422(client, session_factory) -> None:
    run_id = await _insert_scan_run(session_factory)
    rule_id = await _insert_sod_rule(session_factory)
    subject_id = await _insert_subject(session_factory)
    finding_id = await _insert_finding(session_factory, run_id, rule_id, subject_id, 'g')

    resp = await client.patch(f'{_BASE}/{finding_id}/status', json={'status': 'resolved'})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_finding_status_extra_field_returns_422(client, session_factory) -> None:
    run_id = await _insert_scan_run(session_factory)
    rule_id = await _insert_sod_rule(session_factory)
    subject_id = await _insert_subject(session_factory)
    finding_id = await _insert_finding(session_factory, run_id, rule_id, subject_id, 'h')

    resp = await client.patch(
        f'{_BASE}/{finding_id}/status',
        json={'status': 'acknowledged', 'extra_field': 'bad'},
    )
    assert resp.status_code == 422

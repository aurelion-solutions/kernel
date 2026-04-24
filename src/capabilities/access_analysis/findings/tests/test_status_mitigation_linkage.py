# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for mitigation-linkage validation on PATCH /findings/{id}/status → mitigated."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
from src.capabilities.access_analysis.findings.exceptions import (
    FindingMitigationLinkageMissingError,
    FindingMitigationNotApplicableError,
)
from src.capabilities.access_analysis.findings.models import Finding, FindingKind, FindingStatus
from src.capabilities.access_analysis.findings.schemas import FindingStatusPatch
from src.capabilities.access_analysis.findings.service import FindingService
from src.capabilities.access_analysis.mitigation_controls.models import MitigationControl, MitigationControlType
from src.capabilities.access_analysis.mitigations.models import Mitigation, MitigationStatus
from src.capabilities.access_analysis.scan_runs.models import ScanRun, ScanRunTrigger
from src.capabilities.access_analysis.sod_rules.models import SodRule, SodRuleScope, SodSeverity
from src.platform.logs.service import NoOpLogService

_BASE = '/api/v0/findings'
_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(session) -> FindingService:  # type: ignore[no-untyped-def]
    return FindingService(session, NoOpLogService())


async def _seed_run(session) -> int:  # type: ignore[no-untyped-def]
    run = ScanRun(triggered_by=ScanRunTrigger.manual)
    session.add(run)
    await session.flush()
    return run.id


async def _seed_rule(session, scope_key_id: int | None = None) -> int:  # type: ignore[no-untyped-def]
    rule = SodRule(
        code=f'LNK-{uuid.uuid4().hex[:8]}',
        name='Linkage Test Rule',
        severity=SodSeverity.high,
        scope_mode=SodRuleScope.global_,
        mitigation_allowed=True,
    )
    session.add(rule)
    await session.flush()
    return rule.id


async def _seed_subject(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    from src.inventory.nhi.repository import create_nhi
    from src.inventory.subjects.models import SubjectKind, SubjectNHIKind, SubjectNHIStatus
    from src.inventory.subjects.repository import create_subject

    nhi = await create_nhi(
        session,
        external_id=f'lnk-nhi-{uuid.uuid4().hex[:8]}',
        name='Linkage NHI',
        kind='service_account',
    )
    subject = await create_subject(
        session,
        external_id=f'lnk-subj-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status=SubjectNHIStatus.active,
    )
    return subject.id


async def _seed_ctrl(session) -> int:  # type: ignore[no-untyped-def]
    ctrl = MitigationControl(
        code=f'CTRL-{uuid.uuid4().hex[:6]}',
        name='Test Ctrl',
        type=MitigationControlType.attestation,
        is_active=True,
    )
    session.add(ctrl)
    await session.flush()
    return ctrl.id


async def _seed_mitigation(
    session,
    rule_id: int,
    subject_id: uuid.UUID,
    owner_id: uuid.UUID,
    status: MitigationStatus = MitigationStatus.active,
    scope_key_id: int | None = None,
    scope_value: str | None = None,
    valid_from: datetime = _NOW - timedelta(days=1),
    valid_until: datetime | None = None,
) -> int:  # type: ignore[no-untyped-def]
    ctrl_id = await _seed_ctrl(session)
    mit = Mitigation(
        rule_id=rule_id,
        control_id=ctrl_id,
        subject_id=subject_id,
        scope_key_id=scope_key_id,
        scope_value=scope_value,
        status=status,
        valid_from=valid_from,
        valid_until=valid_until,
        owner_id=owner_id,
    )
    session.add(mit)
    await session.flush()
    return mit.id


def _sod_finding(
    run_id: int,
    rule_id: int,
    subject_id: uuid.UUID,
    scope_key_id: int | None = None,
    scope_value: str | None = None,
    hash_suffix: str = '0',
) -> Finding:
    return Finding(
        scan_run_id=run_id,
        kind=FindingKind.sod,
        subject_id=subject_id,
        account_id=None,
        rule_id=rule_id,
        scope_key_id=scope_key_id,
        scope_value=scope_value,
        severity=SodSeverity.high,
        status=FindingStatus.open,
        matched_capability_grant_ids=[],
        matched_effective_grant_ids=[],
        matched_access_fact_ids=[],
        evidence_hash=hash_suffix * 64,
        evaluated_at=_NOW,
    )


# ---------------------------------------------------------------------------
# Service-level tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mitigated_happy_path_exact_scope(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Transition to mitigated with exact-scope mitigation → 200, active_mitigation_id stamped."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        owner_id = await _seed_subject(session)

        sk = CapabilityScopeKey(code=f'LE-{uuid.uuid4().hex[:4]}', name='Legal Entity')
        session.add(sk)
        await session.flush()
        sk_id = sk.id

        mit_id = await _seed_mitigation(
            session,
            rule_id,
            subject_id,
            owner_id,
            scope_key_id=sk_id,
            scope_value='acme',
        )

        f = _sod_finding(run_id, rule_id, subject_id, scope_key_id=sk_id, scope_value='acme', hash_suffix='m')
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
async def test_mitigated_happy_path_unscoped_mitigation(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Transition to mitigated with unscoped mitigation → OK (specific-overrides-generic fallback)."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        owner_id = await _seed_subject(session)

        sk = CapabilityScopeKey(code=f'LE-{uuid.uuid4().hex[:4]}', name='Legal Entity')
        session.add(sk)
        await session.flush()
        sk_id = sk.id

        # Mitigation is unscoped (NULL, NULL) — should cover any finding scope
        mit_id = await _seed_mitigation(
            session,
            rule_id,
            subject_id,
            owner_id,
            scope_key_id=None,
            scope_value=None,
        )

        f = _sod_finding(run_id, rule_id, subject_id, scope_key_id=sk_id, scope_value='acme', hash_suffix='n')
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
async def test_mitigated_missing_active_mitigation_id_raises(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Transition to mitigated without active_mitigation_id (payload nor row) → error."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        f = _sod_finding(run_id, rule_id, subject_id, hash_suffix='p')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingMitigationLinkageMissingError):
            await svc.patch_status(
                finding_id,
                FindingStatusPatch(status=FindingStatus.mitigated),
            )


@pytest.mark.asyncio
async def test_mitigated_nonexistent_mitigation_raises(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Transition to mitigated with non-existent active_mitigation_id → NotApplicable(not found)."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        f = _sod_finding(run_id, rule_id, subject_id, hash_suffix='q')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingMitigationNotApplicableError) as exc_info:
            await svc.patch_status(
                finding_id,
                FindingStatusPatch(status=FindingStatus.mitigated, active_mitigation_id=999999),
            )
    assert exc_info.value.reason == 'not found'


@pytest.mark.asyncio
async def test_mitigated_proposed_mitigation_raises(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Transition to mitigated with proposed-status mitigation → NotApplicable(not active)."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        owner_id = await _seed_subject(session)

        mit_id = await _seed_mitigation(
            session,
            rule_id,
            subject_id,
            owner_id,
            status=MitigationStatus.proposed,
        )

        f = _sod_finding(run_id, rule_id, subject_id, hash_suffix='r')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingMitigationNotApplicableError) as exc_info:
            await svc.patch_status(
                finding_id,
                FindingStatusPatch(status=FindingStatus.mitigated, active_mitigation_id=mit_id),
            )
    assert exc_info.value.reason == 'not active'


@pytest.mark.asyncio
async def test_mitigated_expired_window_raises(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Transition to mitigated with expired mitigation → NotApplicable(expired window)."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        owner_id = await _seed_subject(session)

        past = _NOW - timedelta(days=30)
        expired_until = _NOW - timedelta(days=1)  # expired before 'now'

        mit_id = await _seed_mitigation(
            session,
            rule_id,
            subject_id,
            owner_id,
            status=MitigationStatus.active,
            valid_from=past,
            valid_until=expired_until,
        )

        f = _sod_finding(run_id, rule_id, subject_id, hash_suffix='s')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingMitigationNotApplicableError) as exc_info:
            await svc.patch_status(
                finding_id,
                FindingStatusPatch(status=FindingStatus.mitigated, active_mitigation_id=mit_id),
            )
    assert exc_info.value.reason == 'expired window'


@pytest.mark.asyncio
async def test_mitigated_rule_subject_mismatch_raises(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Transition to mitigated with mitigation for different rule → NotApplicable(rule/subject mismatch)."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        other_rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        owner_id = await _seed_subject(session)

        mit_id = await _seed_mitigation(
            session,
            other_rule_id,
            subject_id,
            owner_id,
            status=MitigationStatus.active,
        )

        f = _sod_finding(run_id, rule_id, subject_id, hash_suffix='t')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingMitigationNotApplicableError) as exc_info:
            await svc.patch_status(
                finding_id,
                FindingStatusPatch(status=FindingStatus.mitigated, active_mitigation_id=mit_id),
            )
    assert exc_info.value.reason == 'rule/subject mismatch'


@pytest.mark.asyncio
async def test_mitigated_scope_mismatch_raises(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Transition to mitigated with mismatched scope mitigation → NotApplicable(scope mismatch)."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        owner_id = await _seed_subject(session)

        sk = CapabilityScopeKey(code=f'LE2-{uuid.uuid4().hex[:4]}', name='Legal Entity 2')
        session.add(sk)
        await session.flush()
        sk_id = sk.id

        # Mitigation with scope (sk, 'other_value'), finding has scope (sk, 'acme')
        mit_id = await _seed_mitigation(
            session,
            rule_id,
            subject_id,
            owner_id,
            status=MitigationStatus.active,
            scope_key_id=sk_id,
            scope_value='other_value',
        )

        f = _sod_finding(run_id, rule_id, subject_id, scope_key_id=sk_id, scope_value='acme', hash_suffix='u')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        with pytest.raises(FindingMitigationNotApplicableError) as exc_info:
            await svc.patch_status(
                finding_id,
                FindingStatusPatch(status=FindingStatus.mitigated, active_mitigation_id=mit_id),
            )
    assert exc_info.value.reason == 'scope mismatch'


@pytest.mark.asyncio
async def test_non_mitigated_transitions_dont_require_mitigation_id(session_factory) -> None:  # type: ignore[no-untyped-def]
    """open → acknowledged and open → resolved do NOT require active_mitigation_id."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)

        f1 = _sod_finding(run_id, rule_id, subject_id, hash_suffix='v')
        f2 = _sod_finding(run_id, rule_id, subject_id, hash_suffix='w')
        session.add(f1)
        session.add(f2)
        await session.flush()
        f1_id = f1.id
        f2_id = f2.id
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        r1 = await svc.patch_status(f1_id, FindingStatusPatch(status=FindingStatus.acknowledged))
        await session.commit()

    async with session_factory() as session:
        svc = _make_service(session)
        r2 = await svc.patch_status(
            f2_id,
            FindingStatusPatch(status=FindingStatus.resolved, status_reason='operator override'),
        )
        await session.commit()

    assert r1.status == FindingStatus.acknowledged
    assert r2.status == FindingStatus.resolved


# ---------------------------------------------------------------------------
# Route-level tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_mitigated_happy_path(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    """PATCH /findings/{id}/status → mitigated with valid mitigation → 200."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        owner_id = await _seed_subject(session)

        mit_id = await _seed_mitigation(session, rule_id, subject_id, owner_id)

        f = _sod_finding(run_id, rule_id, subject_id, hash_suffix='x')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    resp = await client.patch(
        f'{_BASE}/{finding_id}/status',
        json={'status': 'mitigated', 'active_mitigation_id': mit_id},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body['status'] == 'mitigated'
    assert body['active_mitigation_id'] == mit_id


@pytest.mark.asyncio
async def test_route_mitigated_without_id_returns_422(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    """PATCH without active_mitigation_id when transitioning to mitigated → 422."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        f = _sod_finding(run_id, rule_id, subject_id, hash_suffix='y')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    resp = await client.patch(
        f'{_BASE}/{finding_id}/status',
        json={'status': 'mitigated'},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_route_mitigated_not_applicable_returns_422(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    """PATCH with proposed mitigation → 422."""
    async with session_factory() as session:
        run_id = await _seed_run(session)
        rule_id = await _seed_rule(session)
        subject_id = await _seed_subject(session)
        owner_id = await _seed_subject(session)

        mit_id = await _seed_mitigation(
            session,
            rule_id,
            subject_id,
            owner_id,
            status=MitigationStatus.proposed,
        )

        f = _sod_finding(run_id, rule_id, subject_id, hash_suffix='z')
        session.add(f)
        await session.flush()
        finding_id = f.id
        await session.commit()

    resp = await client.patch(
        f'{_BASE}/{finding_id}/status',
        json={'status': 'mitigated', 'active_mitigation_id': mit_id},
    )
    assert resp.status_code == 422
    assert 'not active' in resp.json()['detail']

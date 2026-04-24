# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer integration tests for SodEvaluatorService — DB-backed."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
from src.capabilities.access_analysis.evaluators.service import SodEvaluatorService
from src.capabilities.access_analysis.mitigation_controls.models import MitigationControl, MitigationControlType
from src.capabilities.access_analysis.mitigations.models import Mitigation, MitigationStatus
from src.capabilities.access_analysis.sod_rule_conditions.models import (
    SodRuleCondition,
    sod_rule_condition_capabilities,
)
from src.capabilities.access_analysis.sod_rules.models import SodRule, SodRuleScope, SodSeverity
from src.capabilities.effective_access.models import EffectiveGrant, EffectiveGrantEffect
from src.inventory.access_facts.models import AccessFact, AccessFactEffect
from src.inventory.actions.models import Action as RefAction
from src.inventory.enums import Action
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.nhi.models import NHI
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
from src.platform.applications.models import Application

# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_subject(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    nhi = NHI(
        external_id=f'nhi-{uuid.uuid4().hex[:8]}',
        name=f'test-nhi-{uuid.uuid4().hex[:8]}',
        kind='service_account',
        owner_employee_id=None,
    )
    session.add(nhi)
    await session.flush()
    subject = Subject(
        external_id=f'subj-{uuid.uuid4().hex[:8]}',
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status='active',
    )
    session.add(subject)
    await session.flush()
    return subject.id


async def _seed_application(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    app = Application(
        name=f'app-{uuid.uuid4().hex[:8]}',
        code=f'code-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


async def _seed_capability(session, slug: str) -> int:  # type: ignore[no-untyped-def]
    cap = Capability(slug=slug, name=slug.replace('_', ' ').title())
    session.add(cap)
    await session.flush()
    return cap.id


async def _seed_global_scope_key(session) -> int:  # type: ignore[no-untyped-def]
    sk = CapabilityScopeKey(code=f'GLOBAL-{uuid.uuid4().hex[:4]}', name='Global')
    session.add(sk)
    await session.flush()
    return sk.id


async def _seed_resource(session, app_id: uuid.UUID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    r = Resource(
        external_id=f'ext-{uuid.uuid4().hex[:8]}',
        application_id=app_id,
        kind='role',
        resource_type='role',
        resource_key=f'key-{uuid.uuid4().hex[:8]}',
    )
    session.add(r)
    await session.flush()
    return r.id


async def _seed_eg_chain(
    session,
    subject_id: uuid.UUID,
    app_id: uuid.UUID,
    resource_id: uuid.UUID,
    now: datetime,
    tombstoned_at: datetime | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:  # type: ignore[no-untyped-def]
    """Seed AccessFact → Initiative → EffectiveGrant. Returns (eg_id, fact_id, initiative_id)."""
    read_action_id = (await session.execute(sa.select(RefAction.id).where(RefAction.slug == 'read'))).scalar_one()

    fact = AccessFact(
        subject_id=subject_id,
        resource_id=resource_id,
        action_id=read_action_id,
        effect=AccessFactEffect.allow,
        observed_at=now,
        valid_from=now,
    )
    session.add(fact)
    await session.flush()

    initiative = Initiative(
        access_fact_id=fact.id,
        type=InitiativeType.birthright,
        origin='test-origin',
        valid_from=now,
        valid_until=None,
    )
    session.add(initiative)
    await session.flush()

    eg = EffectiveGrant(
        id=uuid.uuid4(),
        subject_id=subject_id,
        subject_kind=SubjectKind.nhi,
        application_id=app_id,
        resource_id=resource_id,
        action=Action.read,
        effect=EffectiveGrantEffect.allow,
        initiative_type=InitiativeType.birthright,
        initiative_origin='test-origin',
        valid_from=now,
        valid_until=None,
        source_access_fact_id=fact.id,
        source_initiative_id=initiative.id,
        observed_at=now,
        tombstoned_at=tombstoned_at,
    )
    session.add(eg)
    await session.flush()
    return eg.id, fact.id, initiative.id


async def _seed_capability_grant(
    session,
    subject_id: uuid.UUID,
    cap_id: int,
    scope_key_id: int,
    app_id: uuid.UUID,
    eg_id: uuid.UUID,
    mapping_id: int,
    observed_at: datetime,
    tombstoned_at: datetime | None = None,
) -> int:  # type: ignore[no-untyped-def]
    cg = CapabilityGrant(
        subject_id=subject_id,
        capability_id=cap_id,
        scope_key_id=scope_key_id,
        scope_value=None,
        application_id=app_id,
        source_effective_grant_id=eg_id,
        source_capability_mapping_id=mapping_id,
        observed_at=observed_at,
        tombstoned_at=tombstoned_at,
    )
    session.add(cg)
    await session.flush()
    return cg.id


async def _seed_sod_rule_global(session, code: str, cap_id_1: int, cap_id_2: int) -> int:  # type: ignore[no-untyped-def]
    """Seed a GLOBAL SodRule with two conditions (one cap each, min_count=1)."""
    rule = SodRule(
        code=code,
        name=f'Rule {code}',
        severity=SodSeverity.high,
        scope_mode=SodRuleScope.global_,
        mitigation_allowed=True,
        is_enabled=True,
    )
    session.add(rule)
    await session.flush()

    cond1 = SodRuleCondition(rule_id=rule.id, name='cond-a', min_count=1)
    cond2 = SodRuleCondition(rule_id=rule.id, name='cond-b', min_count=1)
    session.add(cond1)
    session.add(cond2)
    await session.flush()

    await session.execute(
        sod_rule_condition_capabilities.insert().values(
            [
                {'condition_id': cond1.id, 'capability_id': cap_id_1},
                {'condition_id': cond2.id, 'capability_id': cap_id_2},
            ]
        )
    )
    await session.flush()
    return rule.id


async def _seed_mitigation_control(session) -> int:  # type: ignore[no-untyped-def]
    ctrl = MitigationControl(
        code=f'CTRL-{uuid.uuid4().hex[:6]}',
        name='Test Control',
        type=MitigationControlType.attestation,
        is_active=True,
    )
    session.add(ctrl)
    await session.flush()
    return ctrl.id


# ---------------------------------------------------------------------------
# Test 24: End-to-end happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_subject_happy_path(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Full DB-backed happy path: seed grants + rule + active mitigation → is_mitigated=True."""
    _NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)

    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)
        sk_id = await _seed_global_scope_key(session)

        cap_id_1 = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        cap_id_2 = await _seed_capability(session, f'approve_payment_{uuid.uuid4().hex[:4]}')

        eg_id_1, _, _ = await _seed_eg_chain(session, subject_id, app_id, resource_id, _NOW)
        eg_id_2, _, _ = await _seed_eg_chain(session, subject_id, app_id, resource_id, _NOW)

        # Use mapping_id=1 as a stub (no FK in CapabilityGrant for mapping)
        await _seed_capability_grant(session, subject_id, cap_id_1, sk_id, app_id, eg_id_1, 1, _NOW)
        await _seed_capability_grant(session, subject_id, cap_id_2, sk_id, app_id, eg_id_2, 2, _NOW)

        rule_id = await _seed_sod_rule_global(session, f'SOD-HAPPY-{uuid.uuid4().hex[:4]}', cap_id_1, cap_id_2)

        ctrl_id = await _seed_mitigation_control(session)
        owner_id = await _seed_subject(session)

        mitigation = Mitigation(
            rule_id=rule_id,
            control_id=ctrl_id,
            subject_id=subject_id,
            scope_key_id=None,
            scope_value=None,
            status=MitigationStatus.active,
            valid_from=_NOW - timedelta(days=1),
            valid_until=None,
            owner_id=owner_id,
        )
        session.add(mitigation)
        await session.flush()
        mitigation_id = mitigation.id

        await session.commit()

    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        violations = await svc.evaluate_subject(subject_id, _NOW)

    assert len(violations) == 1
    v = violations[0]
    assert v.rule_id == rule_id
    assert v.is_mitigated is True
    assert v.active_mitigation_id == mitigation_id
    assert len(v.matched_capability_slugs) == 2
    assert v.evidence_hash  # non-empty

    # Assert no Finding row was inserted
    async with session_factory() as session:
        from src.capabilities.access_analysis.findings.models import Finding

        count = (await session.execute(sa.select(sa.func.count()).select_from(Finding))).scalar_one()
        assert count == 0


# ---------------------------------------------------------------------------
# Test 25: Active-at filtering — tombstoned grant excluded at now, included before tombstone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_active_at_filtering_excludes_tombstoned(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Tombstoned grant excluded at now; visible at timestamp before tombstone."""
    _START = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    _TOMB = datetime(2026, 4, 20, 12, 0, 0, tzinfo=UTC)
    _NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)

    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)
        sk_id = await _seed_global_scope_key(session)

        cap_id_1 = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        cap_id_2 = await _seed_capability(session, f'approve_payment_{uuid.uuid4().hex[:4]}')

        eg_id_1, _, _ = await _seed_eg_chain(session, subject_id, app_id, resource_id, _START)
        eg_id_2, _, _ = await _seed_eg_chain(session, subject_id, app_id, resource_id, _START)

        # Grant 1 is tombstoned
        await _seed_capability_grant(
            session, subject_id, cap_id_1, sk_id, app_id, eg_id_1, 1, _START, tombstoned_at=_TOMB
        )
        # Grant 2 is active
        await _seed_capability_grant(session, subject_id, cap_id_2, sk_id, app_id, eg_id_2, 2, _START)

        await _seed_sod_rule_global(session, f'SOD-TOMB-{uuid.uuid4().hex[:4]}', cap_id_1, cap_id_2)
        await session.commit()

    # Evaluate at _NOW — grant 1 tombstoned → no violation
    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        violations_now = await svc.evaluate_subject(subject_id, _NOW)
    assert len(violations_now) == 0

    # Evaluate BEFORE tombstone — both grants visible → violation
    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        violations_before = await svc.evaluate_subject(subject_id, _TOMB - timedelta(hours=1))
    assert len(violations_before) == 1


# ---------------------------------------------------------------------------
# Test 26: Mitigation validity window filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mitigation_validity_window_filtering(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Mitigation valid_until=2026-04-10; evaluate at 2026-04-09 → mitigated; at 2026-04-15 → not mitigated."""
    _GRANT_AT = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
    _MIT_UNTIL = datetime(2026, 4, 10, 12, 0, 0, tzinfo=UTC)
    _BEFORE = datetime(2026, 4, 9, 12, 0, 0, tzinfo=UTC)
    _AFTER = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)

    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)
        sk_id = await _seed_global_scope_key(session)

        cap_id_1 = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        cap_id_2 = await _seed_capability(session, f'approve_payment_{uuid.uuid4().hex[:4]}')

        eg_id_1, _, _ = await _seed_eg_chain(session, subject_id, app_id, resource_id, _GRANT_AT)
        eg_id_2, _, _ = await _seed_eg_chain(session, subject_id, app_id, resource_id, _GRANT_AT)

        await _seed_capability_grant(session, subject_id, cap_id_1, sk_id, app_id, eg_id_1, 1, _GRANT_AT)
        await _seed_capability_grant(session, subject_id, cap_id_2, sk_id, app_id, eg_id_2, 2, _GRANT_AT)

        rule_id = await _seed_sod_rule_global(session, f'SOD-MITWIN-{uuid.uuid4().hex[:4]}', cap_id_1, cap_id_2)

        ctrl_id = await _seed_mitigation_control(session)
        owner_id = await _seed_subject(session)

        mitigation = Mitigation(
            rule_id=rule_id,
            control_id=ctrl_id,
            subject_id=subject_id,
            scope_key_id=None,
            scope_value=None,
            status=MitigationStatus.active,
            valid_from=_GRANT_AT,
            valid_until=_MIT_UNTIL,
            owner_id=owner_id,
        )
        session.add(mitigation)
        await session.flush()
        await session.commit()

    # Evaluate BEFORE valid_until → mitigation included → is_mitigated=True
    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        violations_before = await svc.evaluate_subject(subject_id, _BEFORE)
    assert len(violations_before) == 1
    assert violations_before[0].is_mitigated is True

    # Evaluate AFTER valid_until → mitigation excluded → is_mitigated=False
    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        violations_after = await svc.evaluate_subject(subject_id, _AFTER)
    assert len(violations_after) == 1
    assert violations_after[0].is_mitigated is False

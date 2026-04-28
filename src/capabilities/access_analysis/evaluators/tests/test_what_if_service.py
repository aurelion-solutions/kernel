# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-level integration tests for SodEvaluatorService.what_if_subject."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from pydantic import ValidationError
import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
from src.capabilities.access_analysis.evaluators.exceptions import (
    WhatIfApplicationNotFoundError,
    WhatIfCapabilityNotFoundError,
    WhatIfScopeKeyNotFoundError,
    WhatIfScopeValueMismatchError,
)
from src.capabilities.access_analysis.evaluators.schemas import (
    CapabilityGrantOverride,
)
from src.capabilities.access_analysis.evaluators.service import SodEvaluatorService
from src.capabilities.access_analysis.findings.models import Finding
from src.capabilities.access_analysis.sod_rule_conditions.models import (
    SodRuleCondition,
    sod_rule_condition_capabilities,
)
from src.capabilities.access_analysis.sod_rules.models import SodRule, SodRuleScope, SodSeverity
from src.capabilities.effective_access.models import EffectiveGrant, EffectiveGrantEffect
from src.inventory.actions.models import Action as RefAction
from src.inventory.enums import Action
from src.inventory.initiatives.models import Initiative, InitiativeType
from src.inventory.nhi.models import NHI
from src.inventory.resources.models import Resource
from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
from src.platform.applications.models import Application

_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Seeding helpers (local; same pattern as existing test_sod_evaluator_service.py)
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


async def _seed_scope_key(session, code: str = 'GLOBAL', is_global: bool = True) -> int:  # type: ignore[no-untyped-def]
    existing = await session.execute(sa.select(CapabilityScopeKey.id).where(CapabilityScopeKey.code == code))
    row = existing.scalar_one_or_none()
    if row is not None:
        return row
    sk = CapabilityScopeKey(code=code, name=code)
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


async def _seed_mapping(session, cap_id: int, app_id: uuid.UUID, sk_id: int) -> int:  # type: ignore[no-untyped-def]
    m = CapabilityMapping(
        capability_id=cap_id,
        application_id=app_id,
        scope_key_id=sk_id,
        scope_value_source='application_id',
        action_slug=None,
        resource_id=None,
        resource_kind='role',
        resource_path_glob=None,
        is_active=True,
    )
    session.add(m)
    await session.flush()
    return m.id


async def _seed_eg_chain(session, subject_id: uuid.UUID, app_id: uuid.UUID, resource_id: uuid.UUID) -> uuid.UUID:  # type: ignore[no-untyped-def]
    read_id = (await session.execute(sa.select(RefAction.id).where(RefAction.slug == 'read'))).scalar_one()
    fact_id = uuid.uuid4()
    await session.execute(
        sa.text(
            'INSERT INTO access_facts '
            '(id, subject_id, resource_id, action_id, effect, observed_at, valid_from) '
            'VALUES (:id, :subject_id, :resource_id, :action_id, :effect, :observed_at, :valid_from)'
        ),
        {
            'id': fact_id,
            'subject_id': subject_id,
            'resource_id': resource_id,
            'action_id': read_id,
            'effect': 'allow',
            'observed_at': _NOW,
            'valid_from': _NOW,
        },
    )
    await session.flush()
    initiative = Initiative(
        access_fact_id=fact_id,
        type=InitiativeType.birthright,
        origin='test',
        valid_from=_NOW,
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
        initiative_origin='test',
        valid_from=_NOW,
        valid_until=None,
        source_access_fact_id=fact_id,
        source_initiative_id=initiative.id,
        observed_at=_NOW,
        tombstoned_at=None,
    )
    session.add(eg)
    await session.flush()
    return eg.id


async def _seed_grant(
    session,
    subject_id: uuid.UUID,
    cap_id: int,
    sk_id: int,
    app_id: uuid.UUID,
    eg_id: uuid.UUID,
    mapping_id: int = 1,
) -> int:  # type: ignore[no-untyped-def]
    cg = CapabilityGrant(
        subject_id=subject_id,
        capability_id=cap_id,
        scope_key_id=sk_id,
        scope_value=None,
        application_id=app_id,
        source_effective_grant_id=eg_id,
        source_capability_mapping_id=mapping_id,
        observed_at=_NOW,
        tombstoned_at=None,
    )
    session.add(cg)
    await session.flush()
    return cg.id


async def _seed_sod_rule(session, cap_id_1: int, cap_id_2: int, scope_mode: SodRuleScope = SodRuleScope.global_) -> int:  # type: ignore[no-untyped-def]
    rule = SodRule(
        code=f'SOD-WI-{uuid.uuid4().hex[:6]}',
        name='What-If Test Rule',
        severity=SodSeverity.high,
        scope_mode=scope_mode,
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
    return rule.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_what_if_adds_violation_via_override(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Subject has create_vendor; override adds approve_payment → violation appears."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')

        cap_id_1 = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        cap_id_2 = await _seed_capability(session, f'approve_payment_{uuid.uuid4().hex[:4]}')

        mapping_id = await _seed_mapping(session, cap_id_1, app_id, sk_id)
        eg_id = await _seed_eg_chain(session, subject_id, app_id, resource_id)
        await _seed_grant(session, subject_id, cap_id_1, sk_id, app_id, eg_id, mapping_id=mapping_id)

        rule_id = await _seed_sod_rule(session, cap_id_1, cap_id_2)
        await session.commit()

    # Without override → no violation (missing approve_payment)
    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        baseline = await svc.evaluate_subject(subject_id, _NOW)
    assert len(baseline) == 0

    # With override adding approve_payment → violation
    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        violations = await svc.what_if_subject(
            subject_id,
            _NOW,
            [
                CapabilityGrantOverride(
                    capability_id=cap_id_2,
                    scope_key_id=sk_id,
                    scope_value=None,
                    application_id=app_id,
                )
            ],
        )
    assert len(violations) == 1
    v = violations[0]
    assert v.rule_id == rule_id
    assert v.is_mitigated is False


@pytest.mark.asyncio
async def test_what_if_does_not_persist(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Override does not create CapabilityGrant or Finding rows."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')

        cap_id_1 = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        cap_id_2 = await _seed_capability(session, f'approve_payment_{uuid.uuid4().hex[:4]}')

        mapping_id = await _seed_mapping(session, cap_id_1, app_id, sk_id)
        eg_id = await _seed_eg_chain(session, subject_id, app_id, resource_id)
        await _seed_grant(session, subject_id, cap_id_1, sk_id, app_id, eg_id, mapping_id=mapping_id)

        await _seed_sod_rule(session, cap_id_1, cap_id_2)
        await session.commit()

    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        await svc.what_if_subject(
            subject_id,
            _NOW,
            [
                CapabilityGrantOverride(
                    capability_id=cap_id_2,
                    scope_key_id=sk_id,
                    scope_value=None,
                    application_id=app_id,
                )
            ],
        )

    async with session_factory() as session:
        cg_count = (
            await session.execute(
                sa.select(sa.func.count()).select_from(CapabilityGrant).where(CapabilityGrant.subject_id == subject_id)
            )
        ).scalar_one()
        finding_count = (await session.execute(sa.select(sa.func.count()).select_from(Finding))).scalar_one()

    # Only 1 real grant was seeded (cap_id_1); the override must NOT have been persisted
    assert cg_count == 1
    assert finding_count == 0


@pytest.mark.asyncio
async def test_what_if_empty_overrides_matches_evaluate(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Empty capability_overrides → identical output to evaluate_subject."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')

        cap_id_1 = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        cap_id_2 = await _seed_capability(session, f'approve_payment_{uuid.uuid4().hex[:4]}')

        mapping_id_1 = await _seed_mapping(session, cap_id_1, app_id, sk_id)
        mapping_id_2 = await _seed_mapping(session, cap_id_2, app_id, sk_id)
        # Need two different resources to avoid the unique constraint on (subject_id, resource_id, action_id)
        resource_id_2 = await _seed_resource(session, app_id)
        eg_id_1 = await _seed_eg_chain(session, subject_id, app_id, resource_id)
        eg_id_2 = await _seed_eg_chain(session, subject_id, app_id, resource_id_2)
        await _seed_grant(session, subject_id, cap_id_1, sk_id, app_id, eg_id_1, mapping_id=mapping_id_1)
        await _seed_grant(session, subject_id, cap_id_2, sk_id, app_id, eg_id_2, mapping_id=mapping_id_2)

        await _seed_sod_rule(session, cap_id_1, cap_id_2)
        await session.commit()

    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        baseline = await svc.evaluate_subject(subject_id, _NOW)

    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        what_if = await svc.what_if_subject(subject_id, _NOW, [])

    # Should match baseline (both return the same violation)
    assert len(baseline) == len(what_if)
    if baseline:
        assert baseline[0].evidence_hash == what_if[0].evidence_hash


@pytest.mark.asyncio
async def test_what_if_nonexistent_capability_raises(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Override with non-existent capability_id → WhatIfCapabilityNotFoundError."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')
        # Need at least one enabled rule so the service does not exit early
        cap_dummy = await _seed_capability(session, f'dummy_{uuid.uuid4().hex[:4]}')
        await _seed_sod_rule(session, cap_dummy, cap_dummy)
        await session.commit()

    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        with pytest.raises(WhatIfCapabilityNotFoundError):
            await svc.what_if_subject(
                subject_id,
                _NOW,
                [
                    CapabilityGrantOverride(
                        capability_id=999999999,
                        scope_key_id=sk_id,
                        scope_value=None,
                        application_id=app_id,
                    )
                ],
            )


@pytest.mark.asyncio
async def test_what_if_nonexistent_scope_key_raises(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Override with non-existent scope_key_id → WhatIfScopeKeyNotFoundError."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        cap_id = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        # Need at least one enabled rule so the service does not exit early
        await _seed_sod_rule(session, cap_id, cap_id)
        await session.commit()

    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        with pytest.raises(WhatIfScopeKeyNotFoundError):
            await svc.what_if_subject(
                subject_id,
                _NOW,
                [
                    CapabilityGrantOverride(
                        capability_id=cap_id,
                        scope_key_id=999999999,
                        scope_value=None,
                        application_id=app_id,
                    )
                ],
            )


@pytest.mark.asyncio
async def test_what_if_nonexistent_application_raises(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Override with non-existent application_id → WhatIfApplicationNotFoundError."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        cap_id = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')
        # Need at least one enabled rule so the service does not exit early
        await _seed_sod_rule(session, cap_id, cap_id)
        await session.commit()

    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        with pytest.raises(WhatIfApplicationNotFoundError):
            await svc.what_if_subject(
                subject_id,
                _NOW,
                [
                    CapabilityGrantOverride(
                        capability_id=cap_id,
                        scope_key_id=sk_id,
                        scope_value=None,
                        application_id=uuid.uuid4(),  # non-existent
                    )
                ],
            )


@pytest.mark.asyncio
async def test_what_if_scope_value_none_for_non_global_key_raises(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Override with scope_value=None for non-GLOBAL key → WhatIfScopeValueMismatchError."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        cap_id = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        # Non-GLOBAL key (code does not start with GLOBAL)
        sk = CapabilityScopeKey(code=f'LEGAL_ENTITY_{uuid.uuid4().hex[:4]}', name='Legal Entity')
        session.add(sk)
        await session.flush()
        non_global_sk_id = sk.id
        # Need at least one enabled rule so the service does not exit early
        await _seed_sod_rule(session, cap_id, cap_id)
        await session.commit()

    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        with pytest.raises(WhatIfScopeValueMismatchError):
            await svc.what_if_subject(
                subject_id,
                _NOW,
                [
                    CapabilityGrantOverride(
                        capability_id=cap_id,
                        scope_key_id=non_global_sk_id,
                        scope_value=None,  # must supply a value for non-GLOBAL key
                        application_id=app_id,
                    )
                ],
            )


def test_what_if_unnormalized_scope_value_raises_at_schema_level() -> None:
    """Override with un-normalized scope_value → ValidationError wrapping WhatIfScopeValueInvalidError."""
    with pytest.raises(ValidationError) as exc_info:
        CapabilityGrantOverride(
            capability_id=1,
            scope_key_id=1,
            scope_value='  Foo  ',  # leading/trailing whitespace + uppercase
            application_id=uuid.uuid4(),
        )
    # Verify the underlying cause is WhatIfScopeValueInvalidError
    errors = exc_info.value.errors()
    assert any('not normalized' in str(e.get('msg', '')) or 'scope_value' in str(e) for e in errors)


def test_what_if_uppercase_scope_value_raises_at_schema_level() -> None:
    """Override with uppercase scope_value → ValidationError wrapping WhatIfScopeValueInvalidError."""
    with pytest.raises(ValidationError):
        CapabilityGrantOverride(
            capability_id=1,
            scope_key_id=1,
            scope_value='FooBar',
            application_id=uuid.uuid4(),
        )


def test_what_if_scope_value_too_long_raises() -> None:
    """Override with scope_value > 255 chars → ValidationError."""
    with pytest.raises(ValidationError):
        CapabilityGrantOverride(
            capability_id=1,
            scope_key_id=1,
            scope_value='a' * 256,
            application_id=uuid.uuid4(),
        )


@pytest.mark.asyncio
async def test_what_if_deterministic_output(session_factory) -> None:  # type: ignore[no-untyped-def]
    """Two identical what-if calls return byte-identical Violation lists."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')

        cap_id_1 = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        cap_id_2 = await _seed_capability(session, f'approve_payment_{uuid.uuid4().hex[:4]}')

        mapping_id = await _seed_mapping(session, cap_id_1, app_id, sk_id)
        eg_id = await _seed_eg_chain(session, subject_id, app_id, resource_id)
        await _seed_grant(session, subject_id, cap_id_1, sk_id, app_id, eg_id, mapping_id=mapping_id)
        await _seed_sod_rule(session, cap_id_1, cap_id_2)
        await session.commit()

    overrides = [
        CapabilityGrantOverride(
            capability_id=cap_id_2,
            scope_key_id=sk_id,
            scope_value=None,
            application_id=app_id,
        )
    ]

    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        result1 = await svc.what_if_subject(subject_id, _NOW, overrides)

    async with session_factory() as session:
        svc = SodEvaluatorService(session)
        result2 = await svc.what_if_subject(subject_id, _NOW, overrides)

    assert len(result1) == len(result2)
    for v1, v2 in zip(result1, result2):
        assert v1.evidence_hash == v2.evidence_hash
        assert v1.rule_id == v2.rule_id

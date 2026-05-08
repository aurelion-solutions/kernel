# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP tests for POST /sod/evaluate."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Test 27: Valid body → 200, correct Violation response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_valid_body_returns_violations(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/evaluate valid body → 200, list of SodViolationResponse."""

    from src.engines.effective_access.models import EffectiveGrant, EffectiveGrantEffect
    from src.inventory.access_model.capabilities.models import Capability
    from src.inventory.access_model.capability_grants.models import CapabilityGrant
    from src.inventory.access_model.capability_mappings.models import CapabilityMapping
    from src.inventory.access_model.capability_scope_keys.models import CapabilityScopeKey
    from src.inventory.enums import Action
    from src.inventory.initiatives.models import Initiative, InitiativeType
    from src.inventory.nhi.models import NHI
    from src.inventory.policy.sod_rule_conditions.models import (
        SodRuleCondition,
        sod_rule_condition_capabilities,
    )
    from src.inventory.policy.sod_rules.models import SodRule, SodRuleScope, SodSeverity
    from src.inventory.resources.models import Resource
    from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
    from src.platform.applications.models import Application

    _NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)

    async with session_factory() as session:
        app = Application(
            name=f'app-{uuid.uuid4().hex[:8]}',
            code=f'code-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app)
        await session.flush()

        nhi = NHI(
            external_id=f'nhi-{uuid.uuid4().hex[:8]}', name='Test', kind='service_account', owner_employee_id=None
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

        resource = Resource(
            external_id=f'ext-{uuid.uuid4().hex[:8]}',
            application_id=app.id,
            kind='role',
            resource_type='role',
            resource_key=f'key-{uuid.uuid4().hex[:8]}',
        )
        session.add(resource)
        await session.flush()

        sk = CapabilityScopeKey(code=f'GLOBAL-{uuid.uuid4().hex[:4]}', name='Global')
        session.add(sk)
        await session.flush()

        cap1 = Capability(slug=f'create_vendor_{uuid.uuid4().hex[:4]}', name='Create Vendor')
        cap2 = Capability(slug=f'approve_payment_{uuid.uuid4().hex[:4]}', name='Approve Payment')
        session.add(cap1)
        session.add(cap2)
        await session.flush()

        fact1_id = uuid.uuid4()
        fact2_id = uuid.uuid4()

        init1 = Initiative(
            access_fact_id=fact1_id, type=InitiativeType.birthright, origin='o1', valid_from=_NOW, valid_until=None
        )
        init2 = Initiative(
            access_fact_id=fact2_id, type=InitiativeType.birthright, origin='o2', valid_from=_NOW, valid_until=None
        )
        session.add(init1)
        session.add(init2)
        await session.flush()

        eg1 = EffectiveGrant(
            id=uuid.uuid4(),
            subject_id=subject.id,
            subject_kind=SubjectKind.nhi,
            application_id=app.id,
            resource_id=resource.id,
            action=Action.read,
            effect=EffectiveGrantEffect.allow,
            initiative_type=InitiativeType.birthright,
            initiative_origin='o1',
            valid_from=_NOW,
            valid_until=None,
            source_access_fact_id=fact1_id,
            source_initiative_id=init1.id,
            observed_at=_NOW,
            tombstoned_at=None,
        )
        eg2 = EffectiveGrant(
            id=uuid.uuid4(),
            subject_id=subject.id,
            subject_kind=SubjectKind.nhi,
            application_id=app.id,
            resource_id=resource.id,
            action=Action.read,
            effect=EffectiveGrantEffect.allow,
            initiative_type=InitiativeType.birthright,
            initiative_origin='o2',
            valid_from=_NOW,
            valid_until=None,
            source_access_fact_id=fact2_id,
            source_initiative_id=init2.id,
            observed_at=_NOW,
            tombstoned_at=None,
        )
        session.add(eg1)
        session.add(eg2)
        await session.flush()

        m1 = CapabilityMapping(
            capability_id=cap1.id,
            application_id=app.id,
            scope_key_id=sk.id,
            scope_value_source={'kind': 'application_id'},
            resource_kind='role',
            is_active=True,
        )
        m2 = CapabilityMapping(
            capability_id=cap2.id,
            application_id=app.id,
            scope_key_id=sk.id,
            scope_value_source={'kind': 'application_id'},
            resource_kind='role',
            is_active=True,
        )
        session.add(m1)
        session.add(m2)
        await session.flush()

        cg1 = CapabilityGrant(
            subject_id=subject.id,
            capability_id=cap1.id,
            scope_key_id=sk.id,
            scope_value=None,
            application_id=app.id,
            source_effective_grant_id=eg1.id,
            source_capability_mapping_id=m1.id,
            observed_at=_NOW,
            tombstoned_at=None,
        )
        cg2 = CapabilityGrant(
            subject_id=subject.id,
            capability_id=cap2.id,
            scope_key_id=sk.id,
            scope_value=None,
            application_id=app.id,
            source_effective_grant_id=eg2.id,
            source_capability_mapping_id=m2.id,
            observed_at=_NOW,
            tombstoned_at=None,
        )
        session.add(cg1)
        session.add(cg2)
        await session.flush()

        rule = SodRule(
            code=f'SOD-ROUTE-{uuid.uuid4().hex[:4]}',
            name='Route Test',
            severity=SodSeverity.high,
            scope_mode=SodRuleScope.global_,
            mitigation_allowed=True,
            is_enabled=True,
        )
        session.add(rule)
        await session.flush()

        cond1 = SodRuleCondition(rule_id=rule.id, name='a', min_count=1)
        cond2 = SodRuleCondition(rule_id=rule.id, name='b', min_count=1)
        session.add(cond1)
        session.add(cond2)
        await session.flush()

        await session.execute(
            sod_rule_condition_capabilities.insert().values(
                [
                    {'condition_id': cond1.id, 'capability_id': cap1.id},
                    {'condition_id': cond2.id, 'capability_id': cap2.id},
                ]
            )
        )
        await session.commit()

        subject_id = subject.id

    resp = await client.post(
        '/api/v0/sod/evaluate',
        json={
            'subject_id': str(subject_id),
            'at': _NOW.isoformat(),
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    v = body[0]
    assert v['is_mitigated'] is False
    assert 'evidence_hash' in v
    assert len(v['evidence_hash']) == 64  # SHA-256 hex
    assert isinstance(v['matched_effective_grant_ids'], list)

    # Assert no Finding row was inserted
    async with session_factory() as session:
        from src.inventory.assessment.findings.models import Finding

        count = (await session.execute(sa.select(sa.func.count()).select_from(Finding))).scalar_one()
        assert count == 0


# ---------------------------------------------------------------------------
# Test 28: Subject with no capabilities → 200 []
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_no_capabilities_returns_empty(client) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/evaluate for subject with no capabilities → 200 []."""
    resp = await client.post(
        '/api/v0/sod/evaluate',
        json={
            'subject_id': str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Test 29: No at field → 200, defaults to now()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_no_at_field_defaults_to_now(client) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/evaluate without at field → 200 (at defaults to now at route boundary)."""
    resp = await client.post(
        '/api/v0/sod/evaluate',
        json={
            'subject_id': str(uuid.uuid4()),
        },
    )
    assert resp.status_code == 200
    assert resp.json() == []


# ---------------------------------------------------------------------------
# Test 30: Extra unknown body field → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_extra_field_returns_422(client) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/evaluate with extra field → 422 (extra='forbid')."""
    resp = await client.post(
        '/api/v0/sod/evaluate',
        json={
            'subject_id': str(uuid.uuid4()),
            'unknown_field': 'boom',
        },
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Test 31: Malformed subject_id → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_malformed_subject_id_returns_422(client) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/evaluate with non-UUID subject_id → 422."""
    resp = await client.post(
        '/api/v0/sod/evaluate',
        json={
            'subject_id': 'not-a-uuid',
        },
    )
    assert resp.status_code == 422

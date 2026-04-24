# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP integration tests for POST /sod/what-if."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
import sqlalchemy as sa
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
from src.capabilities.access_analysis.findings.models import Finding
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

_ENDPOINT = '/api/v0/sod/what-if'
_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Seeding helpers
# ---------------------------------------------------------------------------


async def _seed_subject(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    nhi = NHI(
        external_id=f'nhi-{uuid.uuid4().hex[:8]}',
        name=f'nhi-{uuid.uuid4().hex[:8]}',
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
    cap = Capability(slug=slug, name=slug)
    session.add(cap)
    await session.flush()
    return cap.id


async def _seed_scope_key(session, code: str) -> int:  # type: ignore[no-untyped-def]
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
    fact = AccessFact(
        subject_id=subject_id,
        resource_id=resource_id,
        action_id=read_id,
        effect=AccessFactEffect.allow,
        observed_at=_NOW,
        valid_from=_NOW,
    )
    session.add(fact)
    await session.flush()
    initiative = Initiative(
        access_fact_id=fact.id,
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
        source_access_fact_id=fact.id,
        source_initiative_id=initiative.id,
        observed_at=_NOW,
        tombstoned_at=None,
    )
    session.add(eg)
    await session.flush()
    return eg.id


async def _seed_grant(session, subject_id, cap_id, sk_id, app_id, eg_id, mapping_id):  # type: ignore[no-untyped-def]
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


async def _seed_sod_rule(session, cap_id_1, cap_id_2):  # type: ignore[no-untyped-def]
    rule = SodRule(
        code=f'SOD-WI-RT-{uuid.uuid4().hex[:6]}',
        name='What-If Route Test',
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
async def test_what_if_valid_body_returns_200(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/what-if with valid body → 200, list of SodViolationResponse."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')

        cap_id_1 = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        cap_id_2 = await _seed_capability(session, f'approve_payment_{uuid.uuid4().hex[:4]}')

        mapping_id = await _seed_mapping(session, cap_id_1, app_id, sk_id)
        eg_id = await _seed_eg_chain(session, subject_id, app_id, resource_id)
        await _seed_grant(session, subject_id, cap_id_1, sk_id, app_id, eg_id, mapping_id)
        await _seed_sod_rule(session, cap_id_1, cap_id_2)
        await session.commit()

    resp = await client.post(
        _ENDPOINT,
        json={
            'subject_id': str(subject_id),
            'at': _NOW.isoformat(),
            'capability_overrides': [
                {
                    'capability_id': cap_id_2,
                    'scope_key_id': sk_id,
                    'scope_value': None,
                    'application_id': str(app_id),
                }
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 1
    assert 'evidence_hash' in body[0]
    assert body[0]['is_mitigated'] is False


@pytest.mark.asyncio
async def test_what_if_empty_overrides_returns_200(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/what-if with empty capability_overrides → 200."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)
        resource_id_2 = await _seed_resource(session, app_id)
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')

        cap_id_1 = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        cap_id_2 = await _seed_capability(session, f'approve_payment_{uuid.uuid4().hex[:4]}')

        mapping_id_1 = await _seed_mapping(session, cap_id_1, app_id, sk_id)
        mapping_id_2 = await _seed_mapping(session, cap_id_2, app_id, sk_id)
        eg_id_1 = await _seed_eg_chain(session, subject_id, app_id, resource_id)
        eg_id_2 = await _seed_eg_chain(session, subject_id, app_id, resource_id_2)
        await _seed_grant(session, subject_id, cap_id_1, sk_id, app_id, eg_id_1, mapping_id_1)
        await _seed_grant(session, subject_id, cap_id_2, sk_id, app_id, eg_id_2, mapping_id_2)
        await _seed_sod_rule(session, cap_id_1, cap_id_2)
        await session.commit()

    # Compare evaluate vs. what-if with empty overrides
    eval_resp = await client.post(
        '/api/v0/sod/evaluate',
        json={'subject_id': str(subject_id), 'at': _NOW.isoformat()},
    )
    what_if_resp = await client.post(
        _ENDPOINT,
        json={'subject_id': str(subject_id), 'at': _NOW.isoformat(), 'capability_overrides': []},
    )
    assert eval_resp.status_code == 200
    assert what_if_resp.status_code == 200
    assert len(eval_resp.json()) == len(what_if_resp.json())


@pytest.mark.asyncio
async def test_what_if_extra_field_returns_422(client) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/what-if with extra field in body → 422 (extra='forbid')."""
    resp = await client.post(
        _ENDPOINT,
        json={
            'subject_id': str(uuid.uuid4()),
            'capability_overrides': [],
            'unknown_field': 'boom',
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_what_if_extra_field_in_override_returns_422(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/what-if with extra field inside override → 422."""
    async with session_factory() as session:
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')
        cap_id = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        app_id = await _seed_application(session)
        await session.commit()

    resp = await client.post(
        _ENDPOINT,
        json={
            'subject_id': str(uuid.uuid4()),
            'capability_overrides': [
                {
                    'capability_id': cap_id,
                    'scope_key_id': sk_id,
                    'scope_value': None,
                    'application_id': str(app_id),
                    'extra_field': 'bad',
                }
            ],
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_what_if_nonexistent_capability_returns_422(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/what-if with non-existent capability_id → 422."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')
        # Need at least one enabled rule so the service validates overrides
        cap_dummy = await _seed_capability(session, f'dummy_{uuid.uuid4().hex[:4]}')
        await _seed_sod_rule(session, cap_dummy, cap_dummy)
        await session.commit()

    resp = await client.post(
        _ENDPOINT,
        json={
            'subject_id': str(subject_id),
            'capability_overrides': [
                {
                    'capability_id': 999999999,
                    'scope_key_id': sk_id,
                    'scope_value': None,
                    'application_id': str(app_id),
                }
            ],
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_what_if_does_not_mutate_db(client, session_factory) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/what-if → no new rows in findings or capability_grants."""
    async with session_factory() as session:
        subject_id = await _seed_subject(session)
        app_id = await _seed_application(session)
        resource_id = await _seed_resource(session, app_id)
        sk_id = await _seed_scope_key(session, f'GLOBAL-{uuid.uuid4().hex[:4]}')

        cap_id_1 = await _seed_capability(session, f'create_vendor_{uuid.uuid4().hex[:4]}')
        cap_id_2 = await _seed_capability(session, f'approve_payment_{uuid.uuid4().hex[:4]}')

        mapping_id = await _seed_mapping(session, cap_id_1, app_id, sk_id)
        eg_id = await _seed_eg_chain(session, subject_id, app_id, resource_id)
        await _seed_grant(session, subject_id, cap_id_1, sk_id, app_id, eg_id, mapping_id)
        await _seed_sod_rule(session, cap_id_1, cap_id_2)
        await session.commit()

    # snapshot counts before
    async with session_factory() as session:
        grant_count_before = (
            await session.execute(sa.select(sa.func.count()).select_from(CapabilityGrant))
        ).scalar_one()
        finding_count_before = (await session.execute(sa.select(sa.func.count()).select_from(Finding))).scalar_one()

    resp = await client.post(
        _ENDPOINT,
        json={
            'subject_id': str(subject_id),
            'at': _NOW.isoformat(),
            'capability_overrides': [
                {
                    'capability_id': cap_id_2,
                    'scope_key_id': sk_id,
                    'scope_value': None,
                    'application_id': str(app_id),
                }
            ],
        },
    )
    assert resp.status_code == 200

    async with session_factory() as session:
        grant_count_after = (
            await session.execute(sa.select(sa.func.count()).select_from(CapabilityGrant))
        ).scalar_one()
        finding_count_after = (await session.execute(sa.select(sa.func.count()).select_from(Finding))).scalar_one()

    assert grant_count_after == grant_count_before
    assert finding_count_after == finding_count_before


@pytest.mark.asyncio
async def test_what_if_no_at_defaults_to_now(client) -> None:  # type: ignore[no-untyped-def]
    """POST /sod/what-if without 'at' → 200; at defaults to now(UTC) at route boundary."""
    resp = await client.post(
        _ENDPOINT,
        json={
            'subject_id': str(uuid.uuid4()),
            'capability_overrides': [],
        },
    )
    assert resp.status_code == 200
    # Response is empty list (unknown subject) — we verify the endpoint succeeds.
    # The at-default behaviour is confirmed by the route reading body.at is None → datetime.now(UTC).
    assert resp.json() == []

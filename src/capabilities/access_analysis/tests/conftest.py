# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Shared test fixtures for access_analysis engine tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import sqlalchemy as sa
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
from src.capabilities.access_analysis.scan_runs.models import ScanRun, ScanRunTrigger
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


async def seed_application(session) -> uuid.UUID:
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


async def seed_subject(session, status: str = 'active') -> uuid.UUID:
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
        status=status,
    )
    session.add(subject)
    await session.flush()
    return subject.id


async def seed_capability(session, slug: str) -> int:
    cap = Capability(slug=slug, name=slug.replace('_', ' ').title())
    session.add(cap)
    await session.flush()
    return cap.id


async def seed_scope_key(session, code: str = 'legal_entity') -> int:
    existing = await session.execute(sa.select(CapabilityScopeKey.id).where(CapabilityScopeKey.code == code))
    row = existing.scalar_one_or_none()
    if row is not None:
        return row
    sk = CapabilityScopeKey(code=code, name=code.replace('_', ' ').title())
    session.add(sk)
    await session.flush()
    return sk.id


async def seed_sod_rule(
    session,
    code: str | None = None,
    severity: SodSeverity = SodSeverity.high,
    scope_mode: SodRuleScope = SodRuleScope.global_,
) -> int:
    rule = SodRule(
        code=code or f'RULE-{uuid.uuid4().hex[:8]}',
        name='Test Rule',
        severity=severity,
        scope_mode=scope_mode,
        is_enabled=True,
    )
    session.add(rule)
    await session.flush()
    return rule.id


async def seed_pending_scan_run(session) -> ScanRun:
    run = ScanRun(triggered_by=ScanRunTrigger.manual)
    session.add(run)
    await session.flush()
    await session.refresh(run)
    return run


async def seed_resource(session, app_id: uuid.UUID) -> uuid.UUID:
    r = Resource(
        external_id=f'res-{uuid.uuid4().hex[:8]}',
        application_id=app_id,
        kind='role',
        resource_type='role',
        resource_key=f'key-{uuid.uuid4().hex[:8]}',
    )
    session.add(r)
    await session.flush()
    return r.id


async def seed_effective_grant(session, subject_id: uuid.UUID, app_id: uuid.UUID) -> uuid.UUID:
    """Seed AccessFact → Initiative → EffectiveGrant. Returns eg_id."""
    now = datetime.now(UTC) - timedelta(days=1)
    resource_id = await seed_resource(session, app_id)

    read_action_result = await session.execute(sa.select(RefAction.id).where(RefAction.slug == 'read').limit(1))
    read_action_id = read_action_result.scalar_one()

    fact = AccessFact(
        subject_id=subject_id,
        resource_id=resource_id,
        action_id=read_action_id,
        effect=AccessFactEffect.allow,
        observed_at=now,
        valid_from=now,
        is_active=True,
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
        tombstoned_at=None,
    )
    session.add(eg)
    await session.flush()
    return eg.id


async def seed_capability_grant(
    session,
    subject_id: uuid.UUID,
    capability_id: int,
    app_id: uuid.UUID,
    scope_key_id: int,
    eg_id: uuid.UUID,
    mapping_id: int,
) -> int:
    cg = CapabilityGrant(
        subject_id=subject_id,
        capability_id=capability_id,
        scope_key_id=scope_key_id,
        scope_value=None,
        application_id=app_id,
        source_effective_grant_id=eg_id,
        source_capability_mapping_id=mapping_id,
        observed_at=datetime.now(UTC) - timedelta(days=1),
        tombstoned_at=None,
    )
    session.add(cg)
    await session.flush()
    return cg.id


async def seed_mapping(session, capability_id: int, app_id: uuid.UUID, scope_key_id: int) -> int:
    # resource_kind must be set (XOR constraint: exactly one of resource_id/resource_kind/resource_path_glob)
    m = CapabilityMapping(
        capability_id=capability_id,
        application_id=app_id,
        scope_key_id=scope_key_id,
        scope_value_source='application_id',
        action_slug=None,
        resource_id=None,
        resource_kind='role',  # required: exactly one of resource_id/resource_kind/resource_path_glob
        resource_path_glob=None,
        is_active=True,
    )
    session.add(m)
    await session.flush()
    return m.id

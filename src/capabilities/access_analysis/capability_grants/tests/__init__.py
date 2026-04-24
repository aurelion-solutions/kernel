# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Test helpers for the CapabilityGrant slice."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID


@dataclass(frozen=True)
class Refs:
    """Seeded references for capability_grant tests."""

    subject_id: UUID
    application_id: UUID
    resource_id: UUID
    capability_id: int
    mapping_id: int
    scope_key_global_id: int
    effective_grant_id: UUID


async def _seed_minimal_refs(session) -> Refs:  # type: ignore[no-untyped-def]
    """Seed one each of every prerequisite FK row. Returns Refs dataclass."""
    import sqlalchemy as sa

    from src.capabilities.access_analysis.capabilities.models import Capability
    from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping
    from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey
    from src.capabilities.effective_access.models import EffectiveGrant, EffectiveGrantEffect
    from src.inventory.access_facts.models import AccessFact, AccessFactEffect
    from src.inventory.actions.models import Action as RefAction
    from src.inventory.enums import Action
    from src.inventory.initiatives.models import Initiative, InitiativeType
    from src.inventory.nhi.models import NHI
    from src.inventory.resources.models import Resource
    from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
    from src.platform.applications.models import Application

    now = datetime.now(UTC)

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

    resource = Resource(
        external_id=f'ext-{uuid.uuid4().hex[:8]}',
        application_id=app.id,
        kind='role',
        resource_type='role',
        resource_key=f'key-{uuid.uuid4().hex[:8]}',
    )
    session.add(resource)
    await session.flush()

    cap = Capability(slug=f'cap-{uuid.uuid4().hex[:8]}', name='Test Capability')
    session.add(cap)
    await session.flush()

    global_sk = CapabilityScopeKey(code='GLOBAL', name='Global')
    session.add(global_sk)
    await session.flush()

    mapping = CapabilityMapping(
        capability_id=cap.id,
        resource_kind='role',
        scope_key_id=global_sk.id,
        scope_value_source={'kind': 'constant', 'value': 'admin'},
        is_active=True,
    )
    session.add(mapping)
    await session.flush()

    # Fetch ref_action for 'read'
    read_action_id = (await session.execute(sa.select(RefAction.id).where(RefAction.slug == 'read'))).scalar_one()

    fact = AccessFact(
        subject_id=subject.id,
        resource_id=resource.id,
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
        subject_id=subject.id,
        subject_kind=SubjectKind.nhi,
        application_id=app.id,
        resource_id=resource.id,
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

    return Refs(
        subject_id=subject.id,
        application_id=app.id,
        resource_id=resource.id,
        capability_id=cap.id,
        mapping_id=mapping.id,
        scope_key_global_id=global_sk.id,
        effective_grant_id=eg.id,
    )

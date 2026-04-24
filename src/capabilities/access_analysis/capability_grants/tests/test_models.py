# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ORM-level tests for the CapabilityGrant model."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from src.capabilities.access_analysis.capabilities.models import Capability
from src.capabilities.access_analysis.capability_grants.models import CapabilityGrant
from src.capabilities.access_analysis.capability_mappings.models import CapabilityMapping
from src.capabilities.access_analysis.capability_scope_keys.models import CapabilityScopeKey


async def _seed_prereqs(session):
    """Seed the minimum required FK rows; return (subject_id, app_id, capability_id, scope_key_id, mapping_id)."""
    from src.inventory.nhi.models import NHI
    from src.inventory.resources.models import Resource
    from src.inventory.subjects.models import Subject, SubjectKind, SubjectNHIKind
    from src.platform.applications.models import Application

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

    sk = CapabilityScopeKey(code=f'SK-{uuid.uuid4().hex[:8]}', name='Test SK')
    session.add(sk)
    await session.flush()

    mapping = CapabilityMapping(
        capability_id=cap.id,
        resource_kind='role',
        scope_key_id=sk.id,
        scope_value_source={'kind': 'constant', 'value': 'admin'},
        is_active=True,
    )
    session.add(mapping)
    await session.flush()

    return subject.id, app.id, cap.id, sk.id, mapping.id


@pytest.mark.asyncio
async def test_create_grant_persists_with_all_fields(session_factory) -> None:
    """Insert a CapabilityGrant via session with all required FKs; fetch back; assert columns."""
    async with session_factory() as session:
        subject_id, app_id, cap_id, sk_id, mapping_id = await _seed_prereqs(session)
        eg_id = uuid.uuid4()
        now = datetime.now(UTC)

        grant = CapabilityGrant(
            subject_id=subject_id,
            capability_id=cap_id,
            scope_key_id=sk_id,
            scope_value='some_value',
            application_id=app_id,
            source_effective_grant_id=eg_id,
            source_capability_mapping_id=mapping_id,
            observed_at=now,
            tombstoned_at=None,
        )
        session.add(grant)
        await session.flush()

        fetched = (
            (await session.execute(sa.select(CapabilityGrant).where(CapabilityGrant.id == grant.id))).scalars().one()
        )

        assert fetched.subject_id == subject_id
        assert fetched.capability_id == cap_id
        assert fetched.scope_key_id == sk_id
        assert fetched.scope_value == 'some_value'
        assert fetched.application_id == app_id
        assert fetched.source_effective_grant_id == eg_id
        assert fetched.source_capability_mapping_id == mapping_id
        assert fetched.tombstoned_at is None


@pytest.mark.asyncio
async def test_uq_capability_grants_source_pair_rejects_duplicate(session_factory) -> None:
    """Two rows with the same (source_effective_grant_id, source_capability_mapping_id) raise IntegrityError."""
    async with session_factory() as session:
        subject_id, app_id, cap_id, sk_id, mapping_id = await _seed_prereqs(session)
        eg_id = uuid.uuid4()
        now = datetime.now(UTC)

        grant1 = CapabilityGrant(
            subject_id=subject_id,
            capability_id=cap_id,
            scope_key_id=sk_id,
            scope_value='v1',
            application_id=app_id,
            source_effective_grant_id=eg_id,
            source_capability_mapping_id=mapping_id,
            observed_at=now,
        )
        session.add(grant1)
        await session.flush()

        grant2 = CapabilityGrant(
            subject_id=subject_id,
            capability_id=cap_id,
            scope_key_id=sk_id,
            scope_value='v2',
            application_id=app_id,
            source_effective_grant_id=eg_id,  # same pair
            source_capability_mapping_id=mapping_id,
            observed_at=now,
        )
        session.add(grant2)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_global_scope_value_null_persists(session_factory) -> None:
    """Insert a row with scope_value=None (GLOBAL sentinel); round-trip; assert None preserved."""
    async with session_factory() as session:
        subject_id, app_id, cap_id, _, mapping_id = await _seed_prereqs(session)

        # Use the GLOBAL scope key explicitly
        global_sk = CapabilityScopeKey(code='GLOBAL', name='Global')
        session.add(global_sk)
        await session.flush()

        eg_id = uuid.uuid4()
        now = datetime.now(UTC)

        grant = CapabilityGrant(
            subject_id=subject_id,
            capability_id=cap_id,
            scope_key_id=global_sk.id,
            scope_value=None,  # GLOBAL sentinel
            application_id=app_id,
            source_effective_grant_id=eg_id,
            source_capability_mapping_id=mapping_id,
            observed_at=now,
        )
        session.add(grant)
        await session.flush()

        fetched = (
            (await session.execute(sa.select(CapabilityGrant).where(CapabilityGrant.id == grant.id))).scalars().one()
        )

        assert fetched.scope_value is None
        assert fetched.scope_key_id == global_sk.id

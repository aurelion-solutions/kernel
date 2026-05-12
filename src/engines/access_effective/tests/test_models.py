# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""DB-level tests for EffectiveGrant model — partitioning, constraints, and FK integrity."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from src.engines.access_effective.models import EffectiveGrant, EffectiveGrantEffect
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_employee_subject(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """Create minimal person → employee → subject(employee), return subject.id."""
    from src.inventory.employees.repository import create_employee
    from src.inventory.persons.repository import create_person
    from src.inventory.subjects.models import Subject

    person = await create_person(session, external_id=str(uuid.uuid4()), full_name='test')
    await session.flush()
    emp = await create_employee(session, person_id=person.id)
    await session.flush()
    subj = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.employee,
        principal_employee_id=emp.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def _make_nhi_subject(session) -> uuid.UUID:  # type: ignore[no-untyped-def]
    """Create minimal NHI → subject(nhi), return subject.id."""
    from src.inventory.nhi.models import NHI
    from src.inventory.subjects.models import Subject, SubjectNHIKind

    nhi = NHI(
        external_id=str(uuid.uuid4()),
        name=f'test-nhi-{uuid.uuid4().hex[:8]}',
        kind='service_account',
        owner_employee_id=None,
    )
    session.add(nhi)
    await session.flush()
    subj = Subject(
        external_id=str(uuid.uuid4()),
        kind=SubjectKind.nhi,
        nhi_kind=SubjectNHIKind.service_account,
        principal_nhi_id=nhi.id,
        status='active',
    )
    session.add(subj)
    await session.flush()
    return subj.id


async def _make_application_and_resource(session) -> tuple[uuid.UUID, uuid.UUID]:
    """Create application + resource, return (application_id, resource_id)."""
    from src.inventory.resources.models import Resource
    from src.platform.applications.models import Application

    app = Application(
        name=f'test-app-{uuid.uuid4()}',
        code=f'app-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()

    res_ext = str(uuid.uuid4())
    resource = Resource(
        external_id=res_ext,
        application_id=app.id,
        kind='database',
        resource_type='database',
        resource_key=res_ext,
    )
    session.add(resource)
    await session.flush()
    return app.id, resource.id


async def _make_access_fact(
    session,
    subject_id: uuid.UUID,
    resource_id: uuid.UUID,
) -> uuid.UUID:
    """Synthesize an access_fact UUID.

    Phase 15 Step 16: PG ``access_facts`` table was dropped. ``source_access_fact_id``
    on EffectiveGrant is now a plain UUID with no FK, so we just return a fresh id.
    """
    return uuid.uuid4()


async def _make_initiative(
    session,
    access_fact_id: uuid.UUID,
) -> uuid.UUID:
    """Create Initiative linked to access_fact, return initiative.id."""
    from src.inventory.initiatives.models import Initiative

    initiative = Initiative(
        access_fact_id=access_fact_id,
        type=InitiativeType.birthright,
        origin='test-origin',
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    session.add(initiative)
    await session.flush()
    return initiative.id


def _grant(
    subject_id: uuid.UUID,
    subject_kind: SubjectKind,
    application_id: uuid.UUID,
    resource_id: uuid.UUID,
    source_access_fact_id: uuid.UUID,
    source_initiative_id: uuid.UUID,
    account_id: uuid.UUID | None = None,
) -> EffectiveGrant:
    return EffectiveGrant(
        subject_id=subject_id,
        subject_kind=subject_kind,
        application_id=application_id,
        account_id=account_id,
        resource_id=resource_id,
        action=Action.read,
        effect=EffectiveGrantEffect.allow,
        initiative_type=InitiativeType.birthright,
        initiative_origin='test-origin',
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=None,
        source_access_fact_id=source_access_fact_id,
        source_initiative_id=source_initiative_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_effective_grant_happy_path_persists_and_roundtrips(session_factory) -> None:
    """Happy path: insert one EffectiveGrant, verify it round-trips via session.get."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_application_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        initiative_id = await _make_initiative(session, fact_id)

        grant = _grant(
            subject_id=subject_id,
            subject_kind=SubjectKind.employee,
            application_id=app_id,
            resource_id=resource_id,
            source_access_fact_id=fact_id,
            source_initiative_id=initiative_id,
        )
        session.add(grant)
        await session.flush()
        grant_id = grant.id

        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(EffectiveGrant, (grant_id, SubjectKind.employee, app_id))
        assert fetched is not None
        assert fetched.subject_id == subject_id
        assert fetched.application_id == app_id
        assert fetched.resource_id == resource_id
        assert fetched.effect == EffectiveGrantEffect.allow
        assert fetched.tombstoned_at is None
        assert fetched.observed_at is not None


@pytest.mark.asyncio
async def test_effective_grant_uniqueness_on_source_pair(session_factory) -> None:
    """Two rows with same (source_access_fact_id, source_initiative_id) raise IntegrityError 23505."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_application_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        initiative_id = await _make_initiative(session, fact_id)

        g1 = _grant(
            subject_id=subject_id,
            subject_kind=SubjectKind.employee,
            application_id=app_id,
            resource_id=resource_id,
            source_access_fact_id=fact_id,
            source_initiative_id=initiative_id,
        )
        session.add(g1)
        await session.flush()

        g2 = _grant(
            subject_id=subject_id,
            subject_kind=SubjectKind.employee,
            application_id=app_id,
            resource_id=resource_id,
            source_access_fact_id=fact_id,
            source_initiative_id=initiative_id,
        )
        session.add(g2)
        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        pgcode = getattr(exc_info.value.orig, 'pgcode', None)
        assert pgcode == '23505'


# Phase 15 Step 16: PG access_facts table dropped — source_access_fact_id is now
# a plain UUID with no FK, so the FK-rejection test is no longer applicable.


@pytest.mark.asyncio
async def test_effective_grant_fk_rejects_unknown_source_initiative_id(session_factory) -> None:
    """Inserting with a random source_initiative_id raises IntegrityError 23503."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_application_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)

        grant = _grant(
            subject_id=subject_id,
            subject_kind=SubjectKind.employee,
            application_id=app_id,
            resource_id=resource_id,
            source_access_fact_id=fact_id,
            source_initiative_id=uuid.uuid4(),  # does not exist
        )
        session.add(grant)
        with pytest.raises(IntegrityError) as exc_info:
            await session.flush()
        pgcode = getattr(exc_info.value.orig, 'pgcode', None)
        assert pgcode == '23503'


@pytest.mark.asyncio
async def test_effective_grant_account_id_nullable(session_factory) -> None:
    """EffectiveGrant with account_id=None persists and reads back correctly."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_application_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        initiative_id = await _make_initiative(session, fact_id)

        grant = _grant(
            subject_id=subject_id,
            subject_kind=SubjectKind.employee,
            application_id=app_id,
            resource_id=resource_id,
            source_access_fact_id=fact_id,
            source_initiative_id=initiative_id,
            account_id=None,
        )
        session.add(grant)
        await session.flush()
        grant_id = grant.id
        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(EffectiveGrant, (grant_id, SubjectKind.employee, app_id))
        assert fetched is not None
        assert fetched.account_id is None


@pytest.mark.asyncio
async def test_effective_grant_partition_routing_by_subject_kind(session_factory) -> None:
    """Rows land in the correct LIST partition and are absent from sibling partitions."""
    async with session_factory() as session:
        emp_subject_id = await _make_employee_subject(session)
        nhi_subject_id = await _make_nhi_subject(session)
        app_id, resource_id = await _make_application_and_resource(session)

        emp_fact_id = await _make_access_fact(session, emp_subject_id, resource_id)
        emp_initiative_id = await _make_initiative(session, emp_fact_id)

        nhi_fact_id = await _make_access_fact(session, nhi_subject_id, resource_id)
        nhi_initiative_id = await _make_initiative(session, nhi_fact_id)

        emp_grant = _grant(
            subject_id=emp_subject_id,
            subject_kind=SubjectKind.employee,
            application_id=app_id,
            resource_id=resource_id,
            source_access_fact_id=emp_fact_id,
            source_initiative_id=emp_initiative_id,
        )
        nhi_grant = _grant(
            subject_id=nhi_subject_id,
            subject_kind=SubjectKind.nhi,
            application_id=app_id,
            resource_id=resource_id,
            source_access_fact_id=nhi_fact_id,
            source_initiative_id=nhi_initiative_id,
        )
        session.add(emp_grant)
        session.add(nhi_grant)
        await session.flush()
        emp_id = emp_grant.id
        nhi_id = nhi_grant.id
        await session.commit()

    async with session_factory() as session:
        # Employee row should be in employee partition, absent from nhi partition
        emp_in_employee = await session.execute(
            sa.text('SELECT id FROM effective_grants_employee WHERE id = :id'),
            {'id': emp_id},
        )
        assert emp_in_employee.scalar_one_or_none() is not None

        emp_in_nhi = await session.execute(
            sa.text('SELECT id FROM effective_grants_nhi WHERE id = :id'),
            {'id': emp_id},
        )
        assert emp_in_nhi.scalar_one_or_none() is None

        # NHI row should be in nhi partition, absent from employee partition
        nhi_in_nhi = await session.execute(
            sa.text('SELECT id FROM effective_grants_nhi WHERE id = :id'),
            {'id': nhi_id},
        )
        assert nhi_in_nhi.scalar_one_or_none() is not None

        nhi_in_employee = await session.execute(
            sa.text('SELECT id FROM effective_grants_employee WHERE id = :id'),
            {'id': nhi_id},
        )
        assert nhi_in_employee.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_effective_grants_default_partition_is_trapped(session_factory) -> None:
    """Schema-level: effective_grants_default is the DEFAULT partition and has CHECK (false)."""
    async with session_factory() as session:
        # Verify the DEFAULT partition exists and is attached to the parent
        result = await session.execute(
            sa.text(
                """
                SELECT c.relname AS partition_name
                FROM pg_class c
                JOIN pg_inherits i ON c.oid = i.inhrelid
                JOIN pg_class p ON p.oid = i.inhparent
                JOIN pg_partitioned_table pt ON pt.partrelid = p.oid
                WHERE p.relname = 'effective_grants'
                  AND c.relname = 'effective_grants_default'
                  AND pt.partdefid = c.oid
                """
            )
        )
        row = result.scalar_one_or_none()
        assert row == 'effective_grants_default', 'effective_grants_default is not registered as the DEFAULT partition'

        # Verify the CHECK (false) constraint exists on the default partition
        constraint_result = await session.execute(
            sa.text(
                """
                SELECT con.conname
                FROM pg_constraint con
                JOIN pg_class c ON c.oid = con.conrelid
                WHERE c.relname = 'effective_grants_default'
                  AND con.contype = 'c'
                  AND con.conname = 'ck_effective_grants_default_trap'
                  AND pg_get_constraintdef(con.oid) LIKE '%false%'
                """
            )
        )
        constraint_name = constraint_result.scalar_one_or_none()
        assert constraint_name == 'ck_effective_grants_default_trap', (
            'CHECK (false) trap constraint missing from effective_grants_default'
        )


@pytest.mark.asyncio
async def test_effective_grant_tombstoned_at_defaults_to_null_on_insert(
    session_factory,
) -> None:
    """tombstoned_at is NULL on a freshly inserted EffectiveGrant."""
    async with session_factory() as session:
        subject_id = await _make_employee_subject(session)
        app_id, resource_id = await _make_application_and_resource(session)
        fact_id = await _make_access_fact(session, subject_id, resource_id)
        initiative_id = await _make_initiative(session, fact_id)

        grant = _grant(
            subject_id=subject_id,
            subject_kind=SubjectKind.employee,
            application_id=app_id,
            resource_id=resource_id,
            source_access_fact_id=fact_id,
            source_initiative_id=initiative_id,
        )
        session.add(grant)
        await session.flush()
        await session.refresh(grant)

        assert grant.tombstoned_at is None

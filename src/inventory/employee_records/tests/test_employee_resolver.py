# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for EmployeeResolverService."""

import uuid

import pytest
from sqlalchemy import select
from src.inventory.employee_records.models import (
    EmployeeProviderAttributeMapping,
    EmployeeRecord,
    EmployeeRecordAttribute,
)
from src.inventory.employee_records.repository import get_employee_record_match_by_record_id
from src.inventory.employee_records.resolver import EmployeeResolverService
from src.inventory.employees.models import Employee, EmployeeAttribute
from src.inventory.persons.models import Person
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_direct_determinator_matches_existing_canonical_employee(
    session_factory,
) -> None:
    """Determinator finds Employee via existing canonical attribute."""
    async with session_factory() as session:
        app = Application(name='res-t1', code='res-t1')
        session.add(app)
        await session.flush()
        person = Person(external_id='res-t1-p', full_name='d')
        session.add(person)
        await session.flush()
        canonical = Employee(person_id=person.id)
        session.add(canonical)
        await session.flush()
        session.add(
            EmployeeAttribute(
                employee_id=canonical.id,
                key='work_email',
                value='user@example.com',
            )
        )
        record = EmployeeRecord(external_id='res-t1-r', application_id=app.id)
        session.add(record)
        await session.flush()
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=record.id,
                key='src_email',
                value='user@example.com',
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='src_email',
                employee_key='work_email',
                is_determinator=True,
                allow_upstream=False,
            )
        )
        await session.commit()
        record_id = record.id
        canonical_id = canonical.id

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        out = await resolver.process_employee_record(record_id)
        assert out is not None
        assert out.id == canonical_id


@pytest.mark.asyncio
async def test_direct_determinator_creates_new_canonical_employee(
    session_factory,
) -> None:
    """When determinator value is unknown, a new Employee is created."""
    async with session_factory() as session:
        app = Application(name='res-t2', code='res-t2')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(external_id='res-t2-r', application_id=app.id)
        session.add(record)
        await session.flush()
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=record.id,
                key='src_email',
                value='new@example.com',
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='src_email',
                employee_key='work_email',
                is_determinator=True,
                allow_upstream=False,
            )
        )
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        out = await resolver.process_employee_record(record_id)
        assert out is not None
        row = (
            await session.execute(
                select(EmployeeAttribute).where(
                    EmployeeAttribute.employee_id == out.id,
                    EmployeeAttribute.key == 'work_email',
                )
            )
        ).scalar_one()
        assert row.value == 'new@example.com'


@pytest.mark.asyncio
async def test_upstream_chaining_resolves_via_peer_record(session_factory) -> None:
    """Upstream mapping follows a peer that resolves via determinator."""
    async with session_factory() as session:
        app = Application(name='res-t3', code='res-t3')
        session.add(app)
        await session.flush()
        peer = EmployeeRecord(external_id='res-t3-peer', application_id=app.id)
        leaf = EmployeeRecord(external_id='res-t3-leaf', application_id=app.id)
        session.add_all([peer, leaf])
        await session.flush()
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=peer.id,
                key='src_email',
                value='chain@example.com',
            )
        )
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=peer.id,
                key='link_key',
                value='shared-42',
            )
        )
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=leaf.id,
                key='link_key',
                value='shared-42',
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='src_email',
                employee_key='work_email',
                is_determinator=True,
                allow_upstream=False,
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='link_key',
                employee_key='link_key',
                is_determinator=False,
                allow_upstream=True,
            )
        )
        await session.commit()
        leaf_id = leaf.id
        peer_id = peer.id

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        peer_out = await resolver.process_employee_record(peer_id)
        assert peer_out is not None
        peer_employee_id = peer_out.id
        await session.commit()

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        leaf_out = await resolver.process_employee_record(leaf_id)
        assert leaf_out is not None
        assert leaf_out.id == peer_employee_id
        match = await get_employee_record_match_by_record_id(session, leaf_id)
        assert match is not None
        assert match.matched_via_determinator is False


@pytest.mark.asyncio
async def test_unresolved_record_returns_none(session_factory) -> None:
    """No determinator path and no resolvable upstream yields None."""
    async with session_factory() as session:
        app = Application(name='res-t4', code='res-t4')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(external_id='res-t4-r', application_id=app.id)
        session.add(record)
        await session.flush()
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=record.id,
                key='orphan',
                value='x',
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='src_email',
                employee_key='work_email',
                is_determinator=True,
                allow_upstream=False,
            )
        )
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        assert await resolver.process_employee_record(record_id) is None


@pytest.mark.asyncio
async def test_resolved_record_creates_or_updates_employee_record_match(
    session_factory,
) -> None:
    """Match row is written when resolution succeeds."""
    async with session_factory() as session:
        app = Application(name='res-t5', code='res-t5')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(external_id='res-t5-r', application_id=app.id)
        session.add(record)
        await session.flush()
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=record.id,
                key='src_email',
                value='m@example.com',
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='src_email',
                employee_key='work_email',
                is_determinator=True,
                allow_upstream=False,
            )
        )
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        emp = await resolver.process_employee_record(record_id)
        assert emp is not None
        match = await get_employee_record_match_by_record_id(session, record_id)
        assert match is not None
        assert match.employee_id == emp.id
        assert match.matched_via_determinator is True


@pytest.mark.asyncio
async def test_non_determinator_mapped_attributes_propagate(
    session_factory,
) -> None:
    """Non-determinator mappings copy source attributes to canonical Employee."""
    async with session_factory() as session:
        app = Application(name='res-t6', code='res-t6')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(external_id='res-t6-r', application_id=app.id)
        session.add(record)
        await session.flush()
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=record.id,
                key='src_email',
                value='prop@example.com',
            )
        )
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=record.id,
                key='dept',
                value='Finance',
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='src_email',
                employee_key='work_email',
                is_determinator=True,
                allow_upstream=False,
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='dept',
                employee_key='department',
                is_determinator=False,
                allow_upstream=False,
            )
        )
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        emp = await resolver.process_employee_record(record_id)
        assert emp is not None
        row = (
            await session.execute(
                select(EmployeeAttribute).where(
                    EmployeeAttribute.employee_id == emp.id,
                    EmployeeAttribute.key == 'department',
                )
            )
        ).scalar_one()
        assert row.value == 'Finance'


@pytest.mark.asyncio
async def test_recursion_stops_on_cycle(session_factory) -> None:
    """Upstream cycle does not infinite-loop."""
    async with session_factory() as session:
        app = Application(name='res-t7', code='res-t7')
        session.add(app)
        await session.flush()
        a = EmployeeRecord(external_id='res-t7-a', application_id=app.id)
        b = EmployeeRecord(external_id='res-t7-b', application_id=app.id)
        session.add_all([a, b])
        await session.flush()
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=a.id,
                key='link_key',
                value='cycle',
            )
        )
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=b.id,
                key='link_key',
                value='cycle',
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='src_email',
                employee_key='work_email',
                is_determinator=True,
                allow_upstream=False,
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='link_key',
                employee_key='link_key',
                is_determinator=False,
                allow_upstream=True,
            )
        )
        await session.commit()
        aid = a.id

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        assert await resolver.process_employee_record(aid) is None


@pytest.mark.asyncio
async def test_repeated_processing_is_stable(session_factory) -> None:
    """Second run updates match and attributes consistently."""
    async with session_factory() as session:
        app = Application(name='res-t8', code='res-t8')
        session.add(app)
        await session.flush()
        record = EmployeeRecord(external_id='res-t8-r', application_id=app.id)
        session.add(record)
        await session.flush()
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=record.id,
                key='src_email',
                value='stable@example.com',
            )
        )
        session.add(
            EmployeeRecordAttribute(
                employee_record_id=record.id,
                key='dept',
                value='Eng',
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='src_email',
                employee_key='work_email',
                is_determinator=True,
                allow_upstream=False,
            )
        )
        session.add(
            EmployeeProviderAttributeMapping(
                application_id=app.id,
                employee_record_key='dept',
                employee_key='department',
                is_determinator=False,
                allow_upstream=False,
            )
        )
        await session.commit()
        record_id = record.id

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        first = await resolver.process_employee_record(record_id)
        assert first is not None
        first_id = first.id
        await session.commit()

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        second = await resolver.process_employee_record(record_id)
        assert second is not None
        assert second.id == first_id
        match = await get_employee_record_match_by_record_id(session, record_id)
        assert match is not None
        assert match.employee_id == first_id

    async with session_factory() as session:
        dept_attr = (
            await session.execute(
                select(EmployeeRecordAttribute).where(
                    EmployeeRecordAttribute.employee_record_id == record_id,
                    EmployeeRecordAttribute.key == 'dept',
                )
            )
        ).scalar_one()
        dept_attr.value = 'Legal'
        await session.commit()

    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        third = await resolver.process_employee_record(record_id)
        assert third is not None
        assert third.id == first_id
        row = (
            await session.execute(
                select(EmployeeAttribute).where(
                    EmployeeAttribute.employee_id == first_id,
                    EmployeeAttribute.key == 'department',
                )
            )
        ).scalar_one()
        assert row.value == 'Legal'


@pytest.mark.asyncio
async def test_missing_employee_record_returns_none(session_factory) -> None:
    """Unknown id returns None without raising."""
    async with session_factory() as session:
        resolver = EmployeeResolverService(session)
        assert await resolver.process_employee_record(uuid.uuid4()) is None

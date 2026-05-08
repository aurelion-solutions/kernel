# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for NHI and NHIAttribute models."""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from src.inventory.employees.models import Employee
from src.inventory.nhi.models import (
    NHI,
    NHI_KIND_API_CLIENT,
    NHI_KIND_SERVICE_ACCOUNT,
    NHIAttribute,
)
from src.inventory.persons.models import Person
from src.platform.applications.models import Application


@pytest.mark.asyncio
async def test_create_nhi_with_required_fields(session_factory) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id='nhi-ext-1',
            name='Billing Bot',
            kind=NHI_KIND_SERVICE_ACCOUNT,
        )
        session.add(nhi)
        await session.flush()
        assert nhi.id is not None
        assert nhi.external_id == 'nhi-ext-1'
        assert nhi.name == 'Billing Bot'
        assert nhi.kind == NHI_KIND_SERVICE_ACCOUNT
        assert nhi.description is None
        assert nhi.is_locked is False
        assert nhi.owner_employee_id is None
        assert nhi.application_id is None


@pytest.mark.asyncio
async def test_create_nhi_with_optional_owner_employee_id(session_factory) -> None:
    async with session_factory() as session:
        person = Person(external_id='p-owner', full_name='Owner person')
        session.add(person)
        await session.flush()
        employee = Employee(person_id=person.id, is_locked=False)
        session.add(employee)
        await session.flush()

        nhi = NHI(
            external_id='nhi-owned',
            name='Owned Service',
            kind=NHI_KIND_API_CLIENT,
            owner_employee_id=employee.id,
        )
        session.add(nhi)
        await session.commit()
        nhi_id = nhi.id

    async with session_factory() as session:
        loaded = (await session.execute(select(NHI).where(NHI.id == nhi_id))).scalar_one()
        assert loaded.owner_employee_id == employee.id


@pytest.mark.asyncio
async def test_create_nhi_with_optional_application_id(session_factory) -> None:
    async with session_factory() as session:
        app = Application(name='nhi-app', code='nhi-app', config={})
        session.add(app)
        await session.flush()

        nhi = NHI(
            external_id='nhi-app-linked',
            name='App Client',
            kind=NHI_KIND_API_CLIENT,
            application_id=app.id,
        )
        session.add(nhi)
        await session.commit()
        nhi_id = nhi.id

    async with session_factory() as session:
        loaded = (await session.execute(select(NHI).where(NHI.id == nhi_id))).scalar_one()
        assert loaded.application_id == app.id


@pytest.mark.asyncio
async def test_create_nhi_attribute_linked_to_nhi(session_factory) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id='nhi-attr',
            name='Attr Host',
            kind=NHI_KIND_SERVICE_ACCOUNT,
        )
        session.add(nhi)
        await session.flush()

        attr = NHIAttribute(
            nhi_id=nhi.id,
            key='region',
            value='eu-west-1',
        )
        session.add(attr)
        await session.flush()
        assert attr.id is not None
        assert attr.nhi_id == nhi.id
        assert attr.key == 'region'
        assert attr.value == 'eu-west-1'


@pytest.mark.asyncio
async def test_nhi_attribute_belongs_to_nhi(session_factory) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id='nhi-rel',
            name='Rel Test',
            kind=NHI_KIND_SERVICE_ACCOUNT,
        )
        session.add(nhi)
        await session.flush()
        attr = NHIAttribute(
            nhi_id=nhi.id,
            key='tier',
            value='premium',
        )
        session.add(attr)
        await session.commit()
        nhi_id = nhi.id

    async with session_factory() as session:
        result = await session.execute(select(NHI).where(NHI.id == nhi_id).options(selectinload(NHI.attributes)))
        loaded = result.scalar_one()
        assert len(loaded.attributes) == 1
        assert loaded.attributes[0].key == 'tier'
        assert loaded.attributes[0].nhi is loaded


@pytest.mark.asyncio
async def test_uniqueness_on_nhi_id_key_enforced(session_factory) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id='nhi-dup',
            name='Dup Test',
            kind=NHI_KIND_SERVICE_ACCOUNT,
        )
        session.add(nhi)
        await session.flush()
        attr1 = NHIAttribute(
            nhi_id=nhi.id,
            key='env',
            value='prod',
        )
        session.add(attr1)
        await session.commit()
        nhi_id = nhi.id

    async with session_factory() as session:
        nhi_row = (await session.execute(select(NHI).where(NHI.id == nhi_id))).scalar_one()
        attr2 = NHIAttribute(
            nhi_id=nhi_row.id,
            key='env',
            value='staging',
        )
        session.add(attr2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_nhi_missing_external_id(
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id=None,  # type: ignore[arg-type]
            name='x',
            kind=NHI_KIND_SERVICE_ACCOUNT,
        )
        session.add(nhi)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_nhi_missing_name(
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id='ext',
            name=None,  # type: ignore[arg-type]
            kind=NHI_KIND_SERVICE_ACCOUNT,
        )
        session.add(nhi)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_nhi_missing_kind(
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id='ext',
            name='y',
            kind=None,  # type: ignore[arg-type]
        )
        session.add(nhi)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_nhi_attribute_missing_value(
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id='nhi-req-val',
            name='Req',
            kind=NHI_KIND_SERVICE_ACCOUNT,
        )
        session.add(nhi)
        await session.flush()

        attr = NHIAttribute(
            nhi_id=nhi.id,
            key='k',
            value=None,  # type: ignore[arg-type]
        )
        session.add(attr)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_required_field_behavior_nhi_attribute_missing_key(
    session_factory,
) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id='nhi-req-key',
            name='Req',
            kind=NHI_KIND_SERVICE_ACCOUNT,
        )
        session.add(nhi)
        await session.flush()

        attr = NHIAttribute(
            nhi_id=nhi.id,
            key=None,  # type: ignore[arg-type]
            value='v',
        )
        session.add(attr)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_invalid_owner_employee_id_rejected(session_factory) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id='nhi-bad-emp',
            name='Bad Owner',
            kind=NHI_KIND_SERVICE_ACCOUNT,
            owner_employee_id=uuid.uuid4(),
        )
        session.add(nhi)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_invalid_application_id_rejected(session_factory) -> None:
    async with session_factory() as session:
        nhi = NHI(
            external_id='nhi-bad-app',
            name='Bad App',
            kind=NHI_KIND_SERVICE_ACCOUNT,
            application_id=uuid.uuid4(),
        )
        session.add(nhi)
        with pytest.raises(IntegrityError):
            await session.commit()

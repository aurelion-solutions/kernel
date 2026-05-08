# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Resolve canonical Employee from source EmployeeRecord using provider mappings."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.employee_records.repository import (
    get_employee_record_by_id,
    list_employee_record_attributes,
    list_employee_record_ids_with_attribute_key_value,
    list_provider_attribute_mappings_for_application,
    upsert_employee_record_match,
)
from src.inventory.employees.models import Employee
from src.inventory.employees.repository import (
    create_employee,
    create_employee_attribute,
    find_employee_by_attribute_key_value,
    upsert_employee_attribute,
)
from src.inventory.persons.repository import create_person


class EmployeeResolverService:
    """Maps EmployeeRecord to canonical Employee using determinator and upstream rules."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def process_employee_record(self, employee_record_id: uuid.UUID) -> Employee | None:
        """Resolve canonical Employee for a record, persist match, propagate attributes."""
        record = await get_employee_record_by_id(self._session, employee_record_id)
        if record is None:
            return None
        visited: set[uuid.UUID] = set()
        employee, via_direct = await self._resolve(employee_record_id, visited)
        if employee is None:
            return None
        await upsert_employee_record_match(
            self._session,
            employee_record_id=employee_record_id,
            employee_id=employee.id,
            matched_via_determinator=via_direct,
        )
        await self._propagate_mapped_attributes(employee_record_id, employee.id, record.application_id)
        await self._session.refresh(employee)
        return employee

    async def _attrs_dict(self, employee_record_id: uuid.UUID) -> dict[str, str]:
        rows = await list_employee_record_attributes(self._session, employee_record_id)
        return {a.key: a.value for a in rows}

    async def _create_employee_from_determinator(self, employee_key: str, value: str) -> Employee:
        person = await create_person(
            self._session,
            external_id=f'resolver-{uuid.uuid4()}',
            full_name='resolver-created',
        )
        employee = await create_employee(self._session, person_id=person.id)
        await create_employee_attribute(
            self._session,
            employee_id=employee.id,
            key=employee_key,
            value=value,
        )
        return employee

    async def _direct_determinator(self, record_id: uuid.UUID) -> Employee | None:
        record = await get_employee_record_by_id(self._session, record_id)
        if record is None:
            return None
        attrs = await self._attrs_dict(record_id)
        mappings = await list_provider_attribute_mappings_for_application(
            self._session,
            record.application_id,
            is_determinator=True,
        )
        for m in mappings:
            if m.employee_record_key not in attrs:
                continue
            val = attrs[m.employee_record_key]
            existing = await find_employee_by_attribute_key_value(self._session, key=m.employee_key, value=val)
            if existing is not None:
                return existing
            return await self._create_employee_from_determinator(m.employee_key, val)
        return None

    async def _upstream_peer_record_ids(self, record_id: uuid.UUID) -> list[uuid.UUID]:
        record = await get_employee_record_by_id(self._session, record_id)
        if record is None:
            return []
        attrs = await self._attrs_dict(record_id)
        mappings = await list_provider_attribute_mappings_for_application(
            self._session,
            record.application_id,
            is_determinator=False,
            allow_upstream=True,
        )
        ordered: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        for m in mappings:
            val = attrs.get(m.employee_record_key)
            if val is None:
                continue
            peers = await list_employee_record_ids_with_attribute_key_value(
                self._session,
                key=m.employee_record_key,
                value=val,
                exclude_employee_record_id=record_id,
            )
            for pid in peers:
                if pid not in seen:
                    seen.add(pid)
                    ordered.append(pid)
        return ordered

    async def _resolve(self, record_id: uuid.UUID, visited: set[uuid.UUID]) -> tuple[Employee | None, bool]:
        if record_id in visited:
            return None, False
        visited.add(record_id)
        emp = await self._direct_determinator(record_id)
        if emp is not None:
            return emp, True
        for peer_id in await self._upstream_peer_record_ids(record_id):
            peer_emp, _ = await self._resolve(peer_id, visited)
            if peer_emp is not None:
                return peer_emp, False
        return None, False

    async def _propagate_mapped_attributes(
        self,
        employee_record_id: uuid.UUID,
        employee_id: uuid.UUID,
        application_id: uuid.UUID,
    ) -> None:
        attrs = await self._attrs_dict(employee_record_id)
        mappings = await list_provider_attribute_mappings_for_application(self._session, application_id)
        for m in mappings:
            if m.is_determinator:
                continue
            val = attrs.get(m.employee_record_key)
            if val is None:
                continue
            await upsert_employee_attribute(
                self._session,
                employee_id=employee_id,
                key=m.employee_key,
                value=val,
            )

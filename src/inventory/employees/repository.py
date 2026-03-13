# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Employee repository for PostgreSQL access."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.employees.models import Employee, EmployeeAttribute


async def create_employee(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    is_locked: bool = False,
    description: str | None = None,
) -> Employee:
    """Create and persist an employee."""
    employee = Employee(
        person_id=person_id,
        is_locked=is_locked,
        description=description,
    )
    session.add(employee)
    await session.flush()
    await session.refresh(employee)
    return employee


async def get_employee_by_id(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> Employee | None:
    """Load employee by id."""
    result = await session.execute(select(Employee).where(Employee.id == employee_id))
    return result.scalar_one_or_none()


async def list_employees(session: AsyncSession) -> list[Employee]:
    """List all employees."""
    result = await session.execute(select(Employee).order_by(Employee.id))
    return list(result.scalars().all())


async def list_employee_attributes(
    session: AsyncSession,
    employee_id: uuid.UUID,
) -> list[EmployeeAttribute]:
    """List attributes for an employee."""
    result = await session.execute(
        select(EmployeeAttribute).where(EmployeeAttribute.employee_id == employee_id).order_by(EmployeeAttribute.key)
    )
    return list(result.scalars().all())


async def create_employee_attribute(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    key: str,
    value: str,
) -> EmployeeAttribute:
    """Create and persist an employee attribute."""
    attr = EmployeeAttribute(
        employee_id=employee_id,
        key=key,
        value=value,
    )
    session.add(attr)
    await session.flush()
    await session.refresh(attr)
    return attr


async def get_employee_attribute_by_key(
    session: AsyncSession,
    employee_id: uuid.UUID,
    key: str,
) -> EmployeeAttribute | None:
    """Load employee attribute by employee_id and key."""
    result = await session.execute(
        select(EmployeeAttribute).where(
            EmployeeAttribute.employee_id == employee_id,
            EmployeeAttribute.key == key,
        )
    )
    return result.scalar_one_or_none()


async def find_employee_by_attribute_key_value(
    session: AsyncSession,
    *,
    key: str,
    value: str,
) -> Employee | None:
    """Return the first Employee having a matching canonical attribute key/value."""
    result = await session.execute(
        select(Employee)
        .join(EmployeeAttribute, EmployeeAttribute.employee_id == Employee.id)
        .where(
            EmployeeAttribute.key == key,
            EmployeeAttribute.value == value,
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


async def upsert_employee_attribute(
    session: AsyncSession,
    *,
    employee_id: uuid.UUID,
    key: str,
    value: str,
) -> EmployeeAttribute:
    """Create or update a canonical EmployeeAttribute by key."""
    existing = await get_employee_attribute_by_key(session, employee_id, key)
    if existing is not None:
        existing.value = value
        await session.flush()
        await session.refresh(existing)
        return existing
    return await create_employee_attribute(session, employee_id=employee_id, key=key, value=value)


async def delete_employee_attribute(
    session: AsyncSession,
    employee_id: uuid.UUID,
    key: str,
) -> bool:
    """Delete employee attribute by employee_id and key. Returns True if deleted."""
    attr = await get_employee_attribute_by_key(session, employee_id, key)
    if attr is None:
        return False
    await session.delete(attr)
    return True

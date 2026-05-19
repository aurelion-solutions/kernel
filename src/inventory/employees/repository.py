# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Employee repository for PostgreSQL access."""

from dataclasses import dataclass, field
import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.employees.models import Employee, EmployeeAttribute
from src.inventory.persons.models import Person


@dataclass
class EmployeeUpsertData:
    """Input row for bulk_upsert_employees."""

    person_id: uuid.UUID
    is_locked: bool
    description: str | None
    org_unit_id: uuid.UUID | None
    attributes: dict[str, str] = field(default_factory=dict)


async def create_employee(
    session: AsyncSession,
    *,
    person_id: uuid.UUID,
    is_locked: bool = False,
    description: str | None = None,
    org_unit_id: uuid.UUID | None = None,
) -> Employee:
    """Create and persist an employee."""
    employee = Employee(
        person_id=person_id,
        is_locked=is_locked,
        description=description,
        org_unit_id=org_unit_id,
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


async def list_employees_page(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
) -> tuple[list[Employee], int]:
    """Return (rows, total) for paginated GET /employees.

    Rows are ordered by id ASC and paginated by limit/offset.
    total is the unfiltered row count.
    """
    rows_result = await session.execute(select(Employee).order_by(Employee.id.asc()).limit(limit).offset(offset))
    rows = list(rows_result.scalars().all())

    count_result = await session.execute(select(func.count()).select_from(Employee))
    total: int = count_result.scalar_one()

    return rows, total


async def org_unit_exists(session: AsyncSession, org_unit_id: uuid.UUID) -> bool:
    """Return True if an org_unit row with the given id exists."""
    from src.inventory.org_units.models import OrgUnit  # local import avoids circular at module level

    result = await session.execute(select(OrgUnit.id).where(OrgUnit.id == org_unit_id).limit(1))
    return result.scalar_one_or_none() is not None


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


async def resolve_persons_by_external_ids(
    session: AsyncSession,
    external_ids: list[str],
) -> dict[str, uuid.UUID]:
    """Batch SELECT persons by external_id. Returns {external_id -> person_id} mapping."""
    if not external_ids:
        return {}
    result = await session.execute(select(Person.id, Person.external_id).where(Person.external_id.in_(external_ids)))
    return {row.external_id: row.id for row in result}


async def bulk_upsert_employees(
    session: AsyncSession,
    items: list[EmployeeUpsertData],
) -> list[Employee]:
    """Upsert employees by person_id (ON CONFLICT person_id DO UPDATE).

    Also batch-upserts any attributes provided in each item.

    Returns:
        Employees in the same order as items.

    """
    if not items:
        return []

    values = [
        {
            'person_id': row.person_id,
            'is_locked': row.is_locked,
            'description': row.description,
            'org_unit_id': row.org_unit_id,
        }
        for row in items
    ]

    insert_stmt = pg_insert(Employee).values(values)
    stmt = insert_stmt.on_conflict_do_update(
        constraint='uq_employees_person_id',
        set_={
            'is_locked': insert_stmt.excluded.is_locked,
            'description': insert_stmt.excluded.description,
            'org_unit_id': insert_stmt.excluded.org_unit_id,
        },
    ).returning(Employee)

    result = await session.execute(stmt)
    rows: list[Employee] = list(result.scalars().all())

    index: dict[uuid.UUID, Employee] = {row.person_id: row for row in rows}
    employees = [index[row.person_id] for row in items]

    # Batch-upsert attributes for employees that have them.
    attr_values = [
        {'employee_id': emp.id, 'key': key, 'value': val}
        for emp, data in zip(employees, items)
        for key, val in data.attributes.items()
        if val
    ]
    if attr_values:
        attr_stmt = pg_insert(EmployeeAttribute).values(attr_values)
        attr_stmt = attr_stmt.on_conflict_do_update(
            constraint='uq_employee_attributes_employee_id_key',
            set_={'value': attr_stmt.excluded.value},
        )
        await session.execute(attr_stmt)

    return employees

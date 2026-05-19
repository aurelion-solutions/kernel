# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Migration test: Phase Subject-A backfill — fa1b2c3d4e5f.

Seeds principals without Subject rows, runs the backfill SQL directly,
and asserts that every principal now has exactly one Subject.
Re-running is verified to produce zero additional inserts (idempotency).
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _count_subjects_for_employee(session: AsyncSession, employee_id: uuid.UUID) -> int:
    result = await session.execute(
        sa.text('SELECT count(*) FROM subjects WHERE principal_employee_id = :eid').bindparams(eid=employee_id)
    )
    return result.scalar() or 0


async def _count_subjects_for_nhi(session: AsyncSession, nhi_id: uuid.UUID) -> int:
    result = await session.execute(
        sa.text('SELECT count(*) FROM subjects WHERE principal_nhi_id = :nid').bindparams(nid=nhi_id)
    )
    return result.scalar() or 0


async def _count_subjects_for_customer(session: AsyncSession, customer_id: uuid.UUID) -> int:
    result = await session.execute(
        sa.text('SELECT count(*) FROM subjects WHERE principal_customer_id = :cid').bindparams(cid=customer_id)
    )
    return result.scalar() or 0


_BACKFILL_EMPLOYEE = """
INSERT INTO subjects
    (id, external_id, kind, principal_employee_id, status, created_at, updated_at)
SELECT
    gen_random_uuid(),
    gen_random_uuid()::text,
    'employee',
    e.id,
    'active',
    now(),
    now()
FROM employees e
WHERE NOT EXISTS (
    SELECT 1 FROM subjects s WHERE s.principal_employee_id = e.id
)
"""

_BACKFILL_NHI = """
INSERT INTO subjects
    (id, external_id, kind, nhi_kind, principal_nhi_id, status, created_at, updated_at)
SELECT
    gen_random_uuid(),
    gen_random_uuid()::text,
    'nhi',
    'service_account',
    n.id,
    'active',
    now(),
    now()
FROM nhis n
WHERE NOT EXISTS (
    SELECT 1 FROM subjects s WHERE s.principal_nhi_id = n.id
)
"""

_BACKFILL_CUSTOMER = """
INSERT INTO subjects
    (id, external_id, kind, principal_customer_id, status, created_at, updated_at)
SELECT
    gen_random_uuid(),
    gen_random_uuid()::text,
    'customer',
    c.id,
    'registered',
    now(),
    now()
FROM customers c
WHERE NOT EXISTS (
    SELECT 1 FROM subjects s WHERE s.principal_customer_id = c.id
)
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_creates_missing_subjects(session_factory) -> None:
    """Seeded principals without Subjects get one after the backfill SQL runs."""
    from src.inventory.employees.models import Employee  # noqa: PLC0415
    from src.inventory.nhi.models import NHI  # noqa: PLC0415
    from src.inventory.persons.models import Person  # noqa: PLC0415

    async with session_factory() as session:
        # Create a person + employee with no Subject
        person = Person(external_id=f'mig-p-{uuid.uuid4()}', full_name='Mig Test')
        session.add(person)
        await session.flush()
        employee = Employee(person_id=person.id, is_locked=False)
        session.add(employee)
        await session.flush()
        employee_id = employee.id

        # Create an NHI with no Subject
        nhi = NHI(external_id=f'mig-nhi-{uuid.uuid4()}', name='Mig NHI', kind='service_account')
        session.add(nhi)
        await session.flush()
        nhi_id = nhi.id

        # Verify no Subject exists yet
        assert await _count_subjects_for_employee(session, employee_id) == 0
        assert await _count_subjects_for_nhi(session, nhi_id) == 0

        # Run the backfill SQL (same as migration)
        await session.execute(sa.text(_BACKFILL_EMPLOYEE))
        await session.execute(sa.text(_BACKFILL_NHI))
        await session.flush()

        # Now each principal should have exactly one Subject
        assert await _count_subjects_for_employee(session, employee_id) == 1
        assert await _count_subjects_for_nhi(session, nhi_id) == 1

        await session.rollback()


@pytest.mark.asyncio
async def test_backfill_is_idempotent_when_rerun(session_factory) -> None:
    """Running the backfill SQL twice produces no extra Subject rows."""
    from src.inventory.employees.models import Employee  # noqa: PLC0415
    from src.inventory.persons.models import Person  # noqa: PLC0415

    async with session_factory() as session:
        person = Person(external_id=f'mig-idemp-{uuid.uuid4()}', full_name='Mig Idemp')
        session.add(person)
        await session.flush()
        employee = Employee(person_id=person.id, is_locked=False)
        session.add(employee)
        await session.flush()
        employee_id = employee.id

        # First run
        await session.execute(sa.text(_BACKFILL_EMPLOYEE))
        await session.flush()
        count_after_first = await _count_subjects_for_employee(session, employee_id)

        # Second run — must be a no-op
        await session.execute(sa.text(_BACKFILL_EMPLOYEE))
        await session.flush()
        count_after_second = await _count_subjects_for_employee(session, employee_id)

        assert count_after_first == 1
        assert count_after_second == 1

        await session.rollback()

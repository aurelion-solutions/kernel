# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Subject repository for PostgreSQL access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.employees.models import Employee
from src.inventory.persons.models import Person
from src.inventory.subjects.models import Subject, SubjectAttribute, SubjectKind, SubjectNHIKind, SubjectStatus


async def create_subject(
    session: AsyncSession,
    *,
    external_id: str,
    kind: SubjectKind,
    nhi_kind: SubjectNHIKind | None = None,
    principal_employee_id: uuid.UUID | None = None,
    principal_nhi_id: uuid.UUID | None = None,
    principal_customer_id: uuid.UUID | None = None,
    status: SubjectStatus,
) -> Subject:
    """Create and persist a subject."""
    subject = Subject(
        external_id=external_id,
        kind=kind,
        nhi_kind=nhi_kind,
        principal_employee_id=principal_employee_id,
        principal_nhi_id=principal_nhi_id,
        principal_customer_id=principal_customer_id,
        status=status,
    )
    session.add(subject)
    await session.flush()
    await session.refresh(subject)
    return subject


async def get_subject_by_id(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> Subject | None:
    """Load subject by id."""
    result = await session.execute(select(Subject).where(Subject.id == subject_id))
    return result.scalar_one_or_none()


async def list_subjects(
    session: AsyncSession,
    *,
    kind: SubjectKind | None = None,
    status: SubjectStatus | None = None,
) -> list[Subject]:
    """List subjects with optional filters."""
    query = select(Subject).order_by(Subject.id)
    if kind is not None:
        query = query.where(Subject.kind == kind)
    if status is not None:
        query = query.where(Subject.status == status)
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_subject(
    session: AsyncSession,
    subject: Subject,
    *,
    status: SubjectStatus | None = None,
) -> set[str]:
    """Apply partial update to subject. Returns set of changed field names."""
    changed: set[str] = set()
    if status is not None and subject.status != status:
        subject.status = status
        changed.add('status')
    if changed:
        await session.flush()
        await session.refresh(subject)
    return changed


async def list_subject_attributes(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> list[SubjectAttribute]:
    """List attributes for a subject, ordered by key."""
    result = await session.execute(
        select(SubjectAttribute).where(SubjectAttribute.subject_id == subject_id).order_by(SubjectAttribute.key)
    )
    return list(result.scalars().all())


async def create_subject_attribute(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    key: str,
    value: str,
) -> SubjectAttribute:
    """Create and persist a subject attribute."""
    attr = SubjectAttribute(
        subject_id=subject_id,
        key=key,
        value=value,
    )
    session.add(attr)
    await session.flush()
    await session.refresh(attr)
    return attr


async def get_subject_attribute_by_key(
    session: AsyncSession,
    subject_id: uuid.UUID,
    key: str,
) -> SubjectAttribute | None:
    """Load subject attribute by subject_id and key."""
    result = await session.execute(
        select(SubjectAttribute).where(
            SubjectAttribute.subject_id == subject_id,
            SubjectAttribute.key == key,
        )
    )
    return result.scalar_one_or_none()


async def get_subject_by_principal(
    session: AsyncSession,
    kind: SubjectKind,
    principal_id: uuid.UUID,
) -> Subject | None:
    """Load the Subject bound to the given principal.

    Returns None when no Subject is bound (orphan principal — not an error).
    """
    if kind == SubjectKind.employee:
        col = Subject.principal_employee_id
    elif kind == SubjectKind.nhi:
        col = Subject.principal_nhi_id
    elif kind == SubjectKind.customer:
        col = Subject.principal_customer_id
    else:
        raise ValueError(f'Unknown SubjectKind: {kind!r}')

    result = await session.execute(select(Subject).where(col == principal_id))
    return result.scalar_one_or_none()


async def delete_subject_attribute(
    session: AsyncSession,
    subject_id: uuid.UUID,
    key: str,
) -> bool:
    """Delete subject attribute by subject_id and key. Returns True if deleted."""
    attr = await get_subject_attribute_by_key(session, subject_id, key)
    if attr is None:
        return False
    await session.delete(attr)
    return True


async def resolve_persons_by_external_ids(
    session: AsyncSession,
    external_ids: list[str],
) -> dict[str, uuid.UUID]:
    """Batch SELECT persons by external_id. Returns {external_id -> person_id}."""
    if not external_ids:
        return {}
    result = await session.execute(select(Person.id, Person.external_id).where(Person.external_id.in_(external_ids)))
    return {row.external_id: row.id for row in result}


async def resolve_employees_by_person_ids(
    session: AsyncSession,
    person_ids: list[uuid.UUID],
) -> dict[uuid.UUID, uuid.UUID]:
    """Batch SELECT employees by person_id. Returns {person_id -> employee_id}."""
    if not person_ids:
        return {}
    result = await session.execute(select(Employee.id, Employee.person_id).where(Employee.person_id.in_(person_ids)))
    return {row.person_id: row.id for row in result}


async def find_employee_subjects_excluding_keys(
    session: AsyncSession,
    employee_ids: list[uuid.UUID],
    exclude_external_ids: set[str],
) -> list[Subject]:
    """SELECT subjects where principal_employee_id IN employee_ids AND external_id NOT IN exclude_external_ids.

    Used to detect employee_ids already bound to different subjects (conflict pre-check).
    Returns empty list if employee_ids is empty.
    """
    if not employee_ids:
        return []
    query = select(Subject).where(
        Subject.principal_employee_id.in_(employee_ids),
        Subject.external_id.not_in(exclude_external_ids),
    )
    result = await session.execute(query)
    return list(result.scalars().all())


async def bulk_upsert_employee_subjects(
    session: AsyncSession,
    items_with_employee_ids: list[tuple[str, uuid.UUID, str]],
) -> list[Subject]:
    """Upsert employee-kind subjects by (kind, external_id).

    Args:
        session: SQLAlchemy async session.
        items_with_employee_ids: list of (external_id, employee_id, status)
            tuples in input order.

    Returns:
        Subjects in the same order as items_with_employee_ids.

    Notes:
        kind is hard-coded to 'employee'. principal_nhi_id /
        principal_customer_id / nhi_kind are NULL — required by the
        per-kind CHECK constraints in the model.

    """
    if not items_with_employee_ids:
        return []

    values = [
        {
            'kind': SubjectKind.employee.value,
            'external_id': external_id,
            'principal_employee_id': employee_id,
            'principal_nhi_id': None,
            'principal_customer_id': None,
            'nhi_kind': None,
            'status': status,
        }
        for external_id, employee_id, status in items_with_employee_ids
    ]

    insert_stmt = pg_insert(Subject).values(values)
    stmt = insert_stmt.on_conflict_do_update(
        constraint='uq_subjects_kind_external_id',
        set_={
            'principal_employee_id': insert_stmt.excluded.principal_employee_id,
            'status': insert_stmt.excluded.status,
        },
    ).returning(Subject)

    result = await session.execute(stmt)
    rows: list[Subject] = list(result.scalars().all())

    # Re-order to match input order — RETURNING order is not guaranteed.
    index: dict[str, Subject] = {row.external_id: row for row in rows}
    return [index[external_id] for external_id, _, _ in items_with_employee_ids]

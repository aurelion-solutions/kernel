# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Subject repository for PostgreSQL access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
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

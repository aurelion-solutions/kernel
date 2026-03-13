# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OwnershipAssignment repository for PostgreSQL access."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.ownership_assignments.models import OwnershipAssignment, OwnershipKind


async def create_ownership_assignment(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    resource_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    kind: OwnershipKind,
) -> OwnershipAssignment:
    """Create and persist an ownership assignment."""
    assignment = OwnershipAssignment(
        subject_id=subject_id,
        resource_id=resource_id,
        account_id=account_id,
        kind=kind,
    )
    session.add(assignment)
    await session.flush()
    await session.refresh(assignment)
    return assignment


async def get_ownership_assignment_by_id(
    session: AsyncSession,
    assignment_id: uuid.UUID,
) -> OwnershipAssignment | None:
    """Load ownership assignment by id."""
    result = await session.execute(select(OwnershipAssignment).where(OwnershipAssignment.id == assignment_id))
    return result.scalar_one_or_none()


async def list_ownership_assignments(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    kind: OwnershipKind | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[OwnershipAssignment]:
    """List ownership assignments with optional filters, ordered by created_at DESC."""
    query = select(OwnershipAssignment).order_by(OwnershipAssignment.created_at.desc())
    if subject_id is not None:
        query = query.where(OwnershipAssignment.subject_id == subject_id)
    if resource_id is not None:
        query = query.where(OwnershipAssignment.resource_id == resource_id)
    if account_id is not None:
        query = query.where(OwnershipAssignment.account_id == account_id)
    if kind is not None:
        query = query.where(OwnershipAssignment.kind == kind)
    query = query.limit(min(limit, 200)).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def delete_ownership_assignment(
    session: AsyncSession,
    assignment: OwnershipAssignment,
) -> None:
    """Delete an ownership assignment. Caller must verify existence."""
    await session.delete(assignment)
    await session.flush()

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Mitigation repository — plain async functions over AsyncSession.

No commits. Service flushes; caller commits (per ARCH_CONTEXT transaction-ownership rule).
"""

from __future__ import annotations

from datetime import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.assessment.mitigations.models import Mitigation, MitigationStatus


async def insert_mitigation(
    session: AsyncSession,
    *,
    rule_id: int,
    control_id: int,
    subject_id: uuid.UUID,
    scope_key_id: int | None,
    scope_value: str | None,
    reason: str | None,
    status: MitigationStatus,
    valid_from: datetime,
    valid_until: datetime | None,
    owner_id: uuid.UUID,
    created_by: str | None,
) -> Mitigation:
    """Insert a new Mitigation row and flush. Does not commit."""
    mitigation = Mitigation(
        rule_id=rule_id,
        control_id=control_id,
        subject_id=subject_id,
        scope_key_id=scope_key_id,
        scope_value=scope_value,
        reason=reason,
        status=status,
        valid_from=valid_from,
        valid_until=valid_until,
        owner_id=owner_id,
        created_by=created_by,
    )
    session.add(mitigation)
    await session.flush()
    await session.refresh(mitigation)
    return mitigation


async def get_mitigation_by_id(
    session: AsyncSession,
    mitigation_id: int,
) -> Mitigation | None:
    """Return the Mitigation with the given id, or None."""
    stmt = select(Mitigation).where(Mitigation.id == mitigation_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_mitigations(
    session: AsyncSession,
    *,
    rule_id: int | None = None,
    subject_id: uuid.UUID | None = None,
    status: MitigationStatus | None = None,
    control_id: int | None = None,
    owner_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Mitigation]:
    """Return mitigations ordered by id ASC, optionally filtered."""
    stmt = select(Mitigation).order_by(Mitigation.id.asc())
    if rule_id is not None:
        stmt = stmt.where(Mitigation.rule_id == rule_id)
    if subject_id is not None:
        stmt = stmt.where(Mitigation.subject_id == subject_id)
    if status is not None:
        stmt = stmt.where(Mitigation.status == status)
    if control_id is not None:
        stmt = stmt.where(Mitigation.control_id == control_id)
    if owner_id is not None:
        stmt = stmt.where(Mitigation.owner_id == owner_id)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_mitigation_status_fields(
    session: AsyncSession,
    mitigation: Mitigation,
    *,
    status: MitigationStatus,
    reason: str | None = None,
) -> Mitigation:
    """Update status and optionally reason on a Mitigation, flush, and return the refreshed entity."""
    mitigation.status = status
    if reason is not None:
        mitigation.reason = reason
    await session.flush()
    await session.refresh(mitigation)
    return mitigation


async def get_sod_rule_mitigation_allowed(
    session: AsyncSession,
    rule_id: int,
) -> bool | None:
    """Return mitigation_allowed for the given SodRule, or None if not found.

    Returns None (not False) so the service can distinguish MitigationRuleNotFoundError
    from MitigationRuleNotMitigatableError without importing SodRuleService.
    """
    stmt = (
        sa.select(sa.text('mitigation_allowed'))
        .select_from(sa.table('sod_rules', sa.column('id'), sa.column('mitigation_allowed')))
        .where(sa.column('id') == rule_id)
    )
    result = await session.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None
    return bool(row[0])


async def get_mitigation_control_is_active(
    session: AsyncSession,
    control_id: int,
) -> bool | None:
    """Return is_active for the given MitigationControl, or None if not found."""
    stmt = (
        sa.select(sa.text('is_active'))
        .select_from(sa.table('mitigation_controls', sa.column('id'), sa.column('is_active')))
        .where(sa.column('id') == control_id)
    )
    result = await session.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None
    return bool(row[0])


async def subject_exists(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> bool:
    """Return True if a subject with the given id exists."""
    stmt = (
        sa.select(sa.literal(1)).select_from(sa.table('subjects', sa.column('id'))).where(sa.column('id') == subject_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None

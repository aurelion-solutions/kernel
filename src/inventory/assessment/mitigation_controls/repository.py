# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""MitigationControl repository — plain async functions over AsyncSession.

No commits. Service flushes; caller commits (per ARCH_CONTEXT transaction-ownership rule).
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.assessment.mitigation_controls.models import (
    MitigationControl,
    MitigationControlType,
)


async def insert_mitigation_control(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    description: str | None,
    type: MitigationControlType,
    is_active: bool,
    created_by: str | None,
) -> MitigationControl:
    """Insert a new MitigationControl row and flush. Does not commit."""
    control = MitigationControl(
        code=code,
        name=name,
        description=description,
        type=type,
        is_active=is_active,
        created_by=created_by,
    )
    session.add(control)
    await session.flush()
    await session.refresh(control)
    return control


async def get_mitigation_control_by_id(
    session: AsyncSession,
    control_id: int,
) -> MitigationControl | None:
    """Return the MitigationControl with the given id, or None."""
    stmt = select(MitigationControl).where(MitigationControl.id == control_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_mitigation_controls(
    session: AsyncSession,
    *,
    is_active: bool | None = None,
    type: MitigationControlType | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[MitigationControl]:
    """Return mitigation controls ordered by id ASC, optionally filtered."""
    stmt = select(MitigationControl).order_by(MitigationControl.id.asc())
    if is_active is not None:
        stmt = stmt.where(MitigationControl.is_active == is_active)
    if type is not None:
        stmt = stmt.where(MitigationControl.type == type)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_mitigation_control_fields(
    session: AsyncSession,
    control: MitigationControl,
    *,
    name: str | None = None,
    description: str | None = None,
    type: MitigationControlType | None = None,
    is_active: bool | None = None,
) -> MitigationControl:
    """Update only non-None fields on the control, flush, and return the refreshed entity.

    ``code`` is intentionally absent from this function's signature — codes are immutable.
    """
    if name is not None:
        control.name = name
    if description is not None:
        control.description = description
    if type is not None:
        control.type = type
    if is_active is not None:
        control.is_active = is_active
    await session.flush()
    await session.refresh(control)
    return control

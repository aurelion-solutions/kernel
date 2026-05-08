# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Feedback repository — plain async functions over AsyncSession.

No commits. Service flushes; caller commits (per ARCH_CONTEXT transaction-ownership rule).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.assessment.feedbacks.models import Feedback, FeedbackKind


async def insert_feedback(
    session: AsyncSession,
    *,
    rule_id: int | None,
    capability_mapping_id: int | None,
    finding_id: int | None,
    subject_id: uuid.UUID | None,
    kind: FeedbackKind,
    message: str,
    payload: dict | None,
    created_by: str | None,
) -> Feedback:
    """Insert a new Feedback row and flush. Does not commit."""
    feedback = Feedback(
        rule_id=rule_id,
        capability_mapping_id=capability_mapping_id,
        finding_id=finding_id,
        subject_id=subject_id,
        kind=kind,
        message=message,
        payload=payload,
        created_by=created_by,
    )
    session.add(feedback)
    await session.flush()
    await session.refresh(feedback)
    return feedback


async def get_feedback_by_id(
    session: AsyncSession,
    feedback_id: int,
) -> Feedback | None:
    """Return the Feedback with the given id, or None."""
    stmt = select(Feedback).where(Feedback.id == feedback_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_feedbacks(
    session: AsyncSession,
    *,
    kind: FeedbackKind | None = None,
    rule_id: int | None = None,
    capability_mapping_id: int | None = None,
    finding_id: int | None = None,
    subject_id: uuid.UUID | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Feedback]:
    """Return feedbacks ordered by created_at DESC, optionally filtered."""
    stmt = select(Feedback).order_by(Feedback.created_at.desc())
    if kind is not None:
        stmt = stmt.where(Feedback.kind == kind)
    if rule_id is not None:
        stmt = stmt.where(Feedback.rule_id == rule_id)
    if capability_mapping_id is not None:
        stmt = stmt.where(Feedback.capability_mapping_id == capability_mapping_id)
    if finding_id is not None:
        stmt = stmt.where(Feedback.finding_id == finding_id)
    if subject_id is not None:
        stmt = stmt.where(Feedback.subject_id == subject_id)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def row_exists(
    session: AsyncSession,
    table: str,
    row_id: int,
) -> bool:
    """Return True if a row with the given integer id exists in the named table."""
    stmt = sa.select(sa.literal(1)).select_from(sa.table(table, sa.column('id'))).where(sa.column('id') == row_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def subject_exists(
    session: AsyncSession,
    subject_id: uuid.UUID,
) -> bool:
    """Return True if a subject with the given UUID id exists."""
    stmt = (
        sa.select(sa.literal(1)).select_from(sa.table('subjects', sa.column('id'))).where(sa.column('id') == subject_id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None

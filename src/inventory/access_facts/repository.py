# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact repository for PostgreSQL access."""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_facts.models import AccessFact, AccessFactEffect
from src.inventory.enums import Action


async def create_access_fact(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_id: uuid.UUID,
    action: Action,
    effect: AccessFactEffect,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> AccessFact:
    """Create and persist an access fact."""
    kwargs: dict = {
        'subject_id': subject_id,
        'account_id': account_id,
        'resource_id': resource_id,
        'action': action,
        'effect': effect,
        'valid_until': valid_until,
    }
    if valid_from is not None:
        kwargs['valid_from'] = valid_from
    fact = AccessFact(**kwargs)
    session.add(fact)
    await session.flush()
    await session.refresh(fact)
    return fact


async def get_access_fact_by_id(
    session: AsyncSession,
    fact_id: uuid.UUID,
) -> AccessFact | None:
    """Load access fact by id."""
    result = await session.execute(select(AccessFact).where(AccessFact.id == fact_id))
    return result.scalar_one_or_none()


async def get_access_fact_by_natural_key(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_id: uuid.UUID,
    action: Action,
    effect: AccessFactEffect,
) -> AccessFact | None:
    """Load access fact by natural key (subject, account?, resource, action, effect).

    Uses explicit IS NULL predicate when account_id is None so that the
    NULLS NOT DISTINCT unique constraint is respected correctly.
    """
    from sqlalchemy import and_

    predicates = [
        AccessFact.subject_id == subject_id,
        AccessFact.resource_id == resource_id,
        AccessFact.action == action,
        AccessFact.effect == effect,
    ]
    if account_id is None:
        predicates.append(AccessFact.account_id.is_(None))
    else:
        predicates.append(AccessFact.account_id == account_id)

    result = await session.execute(select(AccessFact).where(and_(*predicates)))
    return result.scalar_one_or_none()


async def list_access_facts(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    action: Action | None = None,
    effect: AccessFactEffect | None = None,
    valid_at: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[AccessFact]:
    """List access facts with optional filters, ordered by created_at DESC."""
    query = select(AccessFact).order_by(AccessFact.created_at.desc())
    if subject_id is not None:
        query = query.where(AccessFact.subject_id == subject_id)
    if resource_id is not None:
        query = query.where(AccessFact.resource_id == resource_id)
    if account_id is not None:
        query = query.where(AccessFact.account_id == account_id)
    if action is not None:
        query = query.where(AccessFact.action == action)
    if effect is not None:
        query = query.where(AccessFact.effect == effect)
    if valid_at is not None:
        query = query.where(AccessFact.valid_from <= valid_at).where(
            (AccessFact.valid_until.is_(None)) | (AccessFact.valid_until > valid_at)
        )
    query = query.limit(min(limit, 200)).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())


async def invalidate_access_fact(
    session: AsyncSession,
    fact: AccessFact,
    *,
    at: datetime,
) -> None:
    """Set valid_until on the fact to mark it as invalidated."""
    fact.valid_until = at
    await session.flush()

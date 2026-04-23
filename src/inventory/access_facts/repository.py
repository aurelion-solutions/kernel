# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact repository for PostgreSQL access."""

from __future__ import annotations

from datetime import datetime
import uuid

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from src.inventory.access_facts.models import AccessFact, AccessFactEffect


async def create_access_fact(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_id: uuid.UUID,
    action_id: int,
    effect: AccessFactEffect,
    observed_at: datetime,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> AccessFact:
    """Create and persist an access fact."""
    kwargs: dict = {
        'subject_id': subject_id,
        'account_id': account_id,
        'resource_id': resource_id,
        'action_id': action_id,
        'effect': effect,
        'observed_at': observed_at,
        'valid_until': valid_until,
        'is_active': True,
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
    *,
    with_action_ref: bool = False,
) -> AccessFact | None:
    """Load access fact by id.

    with_action_ref=True eager-loads action_ref (selectinload) so that
    fact.action_ref.slug is accessible without a lazy-load error.
    """
    q = select(AccessFact).where(AccessFact.id == fact_id)
    if with_action_ref:
        q = q.options(selectinload(AccessFact.action_ref))
    result = await session.execute(q)
    return result.scalar_one_or_none()


async def get_access_fact_by_natural_key(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_id: uuid.UUID,
    action_id: int,
    active_only: bool = True,
) -> AccessFact | None:
    """Load access fact by natural key (subject, account?, resource, action_id).

    When active_only=True (default), adds is_active=True predicate matching the
    partial unique indexes. Returns None for revoked rows when active_only=True.
    """
    predicates = [
        AccessFact.subject_id == subject_id,
        AccessFact.resource_id == resource_id,
        AccessFact.action_id == action_id,
    ]
    if account_id is None:
        predicates.append(AccessFact.account_id.is_(None))
    else:
        predicates.append(AccessFact.account_id == account_id)
    if active_only:
        predicates.append(AccessFact.is_active.is_(True))

    result = await session.execute(select(AccessFact).where(and_(*predicates)))
    return result.scalar_one_or_none()


async def get_revoked_access_fact_by_key(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_id: uuid.UUID,
    action_id: int,
) -> AccessFact | None:
    """Load a revoked (is_active=False) access fact by partial-unique key.

    Used by the reactivation branch in service.create_fact().
    """
    predicates = [
        AccessFact.subject_id == subject_id,
        AccessFact.resource_id == resource_id,
        AccessFact.action_id == action_id,
        AccessFact.is_active.is_(False),
    ]
    if account_id is None:
        predicates.append(AccessFact.account_id.is_(None))
    else:
        predicates.append(AccessFact.account_id == account_id)

    result = await session.execute(
        select(AccessFact).where(and_(*predicates)).order_by(AccessFact.revoked_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def reactivate_access_fact(
    session: AsyncSession,
    fact: AccessFact,
    *,
    effect: AccessFactEffect,
    observed_at: datetime,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
) -> None:
    """Flip is_active=True, clear revoked_at, refresh refreshable fields. One flush."""
    fact.is_active = True
    fact.revoked_at = None
    fact.effect = effect
    fact.observed_at = observed_at
    if valid_from is not None:
        fact.valid_from = valid_from
    fact.valid_until = valid_until
    await session.flush()


async def revoke_access_fact(
    session: AsyncSession,
    fact: AccessFact,
    *,
    revoked_at: datetime,
) -> None:
    """Set is_active=False and stamp revoked_at."""
    fact.is_active = False
    fact.revoked_at = revoked_at
    await session.flush()


async def update_access_fact_fields(
    session: AsyncSession,
    fact: AccessFact,
    *,
    effect: AccessFactEffect,
    valid_from: datetime | None,
    valid_until: datetime | None,
    observed_at: datetime,
) -> None:
    """Update mutable fields (effect, valid_from, valid_until, observed_at) in place. One flush.

    valid_from is NOT NULL in the schema; if the caller passes None we preserve
    the existing value (no-op for that field).
    """
    fact.effect = effect
    if valid_from is not None:
        fact.valid_from = valid_from  # type: ignore[assignment]
    fact.valid_until = valid_until
    fact.observed_at = observed_at
    await session.flush()


async def list_access_facts(
    session: AsyncSession,
    *,
    subject_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    action_id: int | None = None,
    effect: AccessFactEffect | None = None,
    is_active: bool | None = None,
    valid_at: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
    with_action_ref: bool = False,
) -> list[AccessFact]:
    """List access facts with optional filters, ordered by created_at DESC."""
    query = select(AccessFact).order_by(AccessFact.created_at.desc())
    if with_action_ref:
        query = query.options(selectinload(AccessFact.action_ref))
    if subject_id is not None:
        query = query.where(AccessFact.subject_id == subject_id)
    if resource_id is not None:
        query = query.where(AccessFact.resource_id == resource_id)
    if account_id is not None:
        query = query.where(AccessFact.account_id == account_id)
    if action_id is not None:
        query = query.where(AccessFact.action_id == action_id)
    if effect is not None:
        query = query.where(AccessFact.effect == effect)
    if is_active is not None:
        query = query.where(AccessFact.is_active == is_active)
    if valid_at is not None:
        query = query.where(AccessFact.valid_from <= valid_at).where(
            (AccessFact.valid_until.is_(None)) | (AccessFact.valid_until > valid_at)
        )
    query = query.limit(min(limit, 200)).offset(offset)
    result = await session.execute(query)
    return list(result.scalars().all())

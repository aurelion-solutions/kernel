# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Account repository."""

import uuid

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.accounts.models import Account, AccountStatus


async def get_account_by_id(
    session: AsyncSession,
    account_id: uuid.UUID,
) -> Account | None:
    """Fetch a single account by primary key."""
    result = await session.execute(select(Account).where(Account.id == account_id))
    return result.scalar_one_or_none()


async def list_accounts(
    session: AsyncSession,
    *,
    application_id: uuid.UUID | None = None,
    status: AccountStatus | None = None,
    subject_id: uuid.UUID | None = None,
) -> list[Account]:
    """List accounts with optional filters."""
    query = select(Account).order_by(Account.id)
    if application_id is not None:
        query = query.where(Account.application_id == application_id)
    if status is not None:
        query = query.where(Account.status == status)
    if subject_id is not None:
        query = query.where(Account.subject_id == subject_id)
    result = await session.execute(query)
    return list(result.scalars().all())


async def update_account(
    session: AsyncSession,
    account: Account,
    *,
    status: AccountStatus | None = None,
    subject_id: uuid.UUID | None = None,
) -> set[str]:
    """Apply partial update to account fields. Returns the set of changed field names."""
    changed: set[str] = set()
    if status is not None and account.status != status:
        account.status = status
        changed.add('status')
    if subject_id is not None and account.subject_id != subject_id:
        account.subject_id = subject_id
        changed.add('subject_id')
    if changed:
        await session.flush()
        await session.refresh(account)
    return changed


async def list_by_application(
    session: AsyncSession,
    application_id: uuid.UUID,
) -> list[Account]:
    """Load all accounts for the given application. Shim over list_accounts."""
    return await list_accounts(session, application_id=application_id)


async def upsert_accounts_bulk(
    session: AsyncSession,
    items: list[dict],
) -> int:
    """INSERT ... ON CONFLICT (application_id, username) DO UPDATE display_name, email.

    Returns rowcount as reported by the driver (inserts + updates).
    """
    if not items:
        return 0
    stmt = pg_insert(Account).values(items)
    stmt = stmt.on_conflict_do_update(
        index_elements=['application_id', 'username'],
        set_={
            'display_name': stmt.excluded.display_name,
            'email': stmt.excluded.email,
        },
    )
    result = await session.execute(stmt)
    return result.rowcount

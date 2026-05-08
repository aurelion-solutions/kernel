# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Account service for coordinating repository and event emission."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.accounts.models import Account, AccountStatus
from src.inventory.accounts.repository import (
    get_account_by_id as repo_get_account_by_id,
)
from src.inventory.accounts.repository import (
    list_accounts as repo_list_accounts,
)
from src.inventory.accounts.repository import (
    update_account as repo_update_account,
)
from src.inventory.accounts.repository import (
    upsert_accounts_bulk as repo_upsert_accounts_bulk,
)
from src.inventory.accounts.schemas import AccountBulkItem, AccountPatch
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service

_COMPONENT = 'inventory.accounts'


class AccountNotFoundError(Exception):
    """Raised when an account is not found."""

    def __init__(self, account_id: uuid.UUID) -> None:
        self.account_id = account_id
        super().__init__(f'Account not found: {account_id}')


class AccountSubjectNotFoundError(Exception):
    """FK violation (pgcode 23503) on Account.subject_id → subjects.id."""

    def __init__(self, subject_id: uuid.UUID | None = None) -> None:
        self.subject_id = subject_id
        super().__init__('Referenced subject does not exist')


class AccountService:
    """Orchestrates account read/list/update and event emission."""

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def get_account(
        self,
        session: AsyncSession,
        account_id: uuid.UUID,
    ) -> Account | None:
        """Get account by id. No event emitted (Q1 — read-side audit deferred to future audit.* slice)."""
        return await repo_get_account_by_id(session, account_id)

    async def list_accounts(
        self,
        session: AsyncSession,
        *,
        application_id: uuid.UUID | None = None,
        status: AccountStatus | None = None,
        subject_id: uuid.UUID | None = None,
    ) -> list[Account]:
        """List accounts with optional filters. No event emitted."""
        return await repo_list_accounts(
            session,
            application_id=application_id,
            status=status,
            subject_id=subject_id,
        )

    async def update_account(
        self,
        session: AsyncSession,
        account_id: uuid.UUID,
        patch: AccountPatch,
        correlation_id: str | None = None,
    ) -> Account:
        """Apply partial update and emit inventory.account.updated if fields changed."""
        account = await repo_get_account_by_id(session, account_id)
        if account is None:
            raise AccountNotFoundError(account_id)

        try:
            changed = await repo_update_account(
                session,
                account,
                status=patch.status,
                subject_id=patch.subject_id,
            )
        except IntegrityError as exc:
            pgcode = getattr(exc.orig, 'pgcode', None) or getattr(exc.orig, 'sqlstate', None)
            if pgcode == '23503':
                raise AccountSubjectNotFoundError(patch.subject_id) from None
            raise

        if changed:
            await self._events.emit(
                EventEnvelope(
                    event_id=uuid.uuid4(),
                    event_type='inventory.account.updated',
                    occurred_at=datetime.now(UTC),
                    correlation_id=correlation_id if correlation_id is not None else uuid.uuid4().hex,
                    causation_id=None,
                    payload={
                        'account_id': str(account_id),
                        'changed_fields': sorted(changed),
                    },
                    actor_kind=EventParticipantKind.COMPONENT,
                    actor_id=_COMPONENT,
                    target_kind=EventParticipantKind.SYSTEM,
                    target_id=str(account.id),
                )
            )
        return account

    async def upsert_bulk(
        self,
        session: AsyncSession,
        items: list[AccountBulkItem],
        *,
        correlation_id: str | None = None,  # noqa: ARG002 — reserved for future event emission
    ) -> int:
        """Bulk upsert accounts by (application_id, username). Returns rowcount."""
        rows = [
            {
                'application_id': item.application_id,
                'username': item.username,
                'display_name': item.display_name,
                'email': item.email,
            }
            for item in items
        ]
        return await repo_upsert_accounts_bulk(session, rows)

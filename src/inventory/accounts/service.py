# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Account service for coordinating repository and log emission."""

from __future__ import annotations

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
from src.inventory.accounts.schemas import AccountPatch
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service

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
    """Orchestrates account read/list/update and log emission."""

    def __init__(self, log_service: LogService | None = None) -> None:
        self._log = log_service if log_service is not None else noop_log_service

    async def get_account(
        self,
        session: AsyncSession,
        account_id: uuid.UUID,
    ) -> Account | None:
        """Get account by id. Emits account.retrieved when found."""
        account = await repo_get_account_by_id(session, account_id)
        if account is not None:
            self._log.emit_safe(
                'account.retrieved',
                LogLevel.INFO,
                'Account retrieved',
                _COMPONENT,
                merge_emit_log_participant_fields(
                    {'account_id': str(account_id)},
                    actor_component=_COMPONENT,
                    target_id='account',
                ),
            )
        return account

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
    ) -> Account:
        """Apply partial update and emit account.updated if fields changed."""
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
            self._log.emit_safe(
                'account.updated',
                LogLevel.INFO,
                'Account updated',
                _COMPONENT,
                merge_emit_log_participant_fields(
                    {
                        'account_id': str(account_id),
                        'changed_fields': sorted(changed),
                    },
                    actor_component=_COMPONENT,
                    target_id='account',
                ),
            )
        return account

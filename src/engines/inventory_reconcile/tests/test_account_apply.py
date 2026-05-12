# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for master_data_apply.apply_accounts_delta.

Verifies that account delta items are correctly applied to the ent_accounts table.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from src.engines.inventory_reconcile.master_data_apply import (
    MasterDataApplyResult,
    apply_accounts_delta,
    apply_master_data_delta,
)
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
    ReconciliationRunStatus,
)
from src.engines.inventory_reconcile.repository import create_run
from src.inventory.accounts.models import Account, AccountStatus


async def _make_app(session) -> uuid.UUID:
    from src.platform.applications.models import Application  # noqa: PLC0415

    app = Application(
        name=f'acct-apply-test-{uuid.uuid4()}',
        code=f'aat-{uuid.uuid4().hex[:8]}',
        config={},
        required_connector_tags=[],
        is_active=True,
    )
    session.add(app)
    await session.flush()
    return app.id


def _account_item(
    run_id: uuid.UUID,
    operation: ReconciliationDeltaOperation,
    *,
    before_json=None,
    after_json=None,
    entity_id=None,
) -> ReconciliationDeltaItem:
    return ReconciliationDeltaItem(
        reconciliation_run_id=run_id,
        entity_type=ReconciliationEntityType.account,
        operation=operation,
        entity_id=entity_id,
        before_json=before_json,
        after_json=after_json,
    )


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_account_create_inserts_row(session_factory):
    """CREATE delta → new Account row in PG with correct fields."""
    async with session_factory() as session:
        app_id = await _make_app(session)
        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.account)
        item = _account_item(
            run.id,
            ReconciliationDeltaOperation.create,
            after_json={
                'application_id': str(app_id),
                'username': 'new.hire1@company.com',
                'email': 'new.hire1@company.com',
                'display_name': 'New Hire 1',
                'status': 'active',
                'is_privileged': False,
                'mfa_enabled': True,
            },
        )
        session.add(item)
        await session.flush()

        result = await apply_accounts_delta(session, run_id=run.id)
        await session.commit()

    assert isinstance(result, MasterDataApplyResult)
    assert result.applied_count == 1
    assert result.failed_count == 0

    async with session_factory() as session:
        row = await session.execute(
            sa.select(Account).where(
                Account.application_id == app_id,
                Account.username == 'new.hire1@company.com',
            )
        )
        account = row.scalar_one()
        assert account.display_name == 'New Hire 1'
        assert account.mfa_enabled is True
        assert account.status == AccountStatus.active


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_account_update_changes_fields(session_factory):
    """UPDATE delta → existing Account fields updated in PG."""
    async with session_factory() as session:
        app_id = await _make_app(session)
        account = Account(
            application_id=app_id,
            username='existing.user',
            display_name='Old Name',
            mfa_enabled=False,
            status=AccountStatus.active,
        )
        session.add(account)
        await session.flush()
        account_id = account.id

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.account)
        item = _account_item(
            run.id,
            ReconciliationDeltaOperation.update,
            entity_id=account_id,
            before_json={'display_name': 'Old Name', 'mfa_enabled': False},
            after_json={'display_name': 'New Name', 'mfa_enabled': True},
        )
        session.add(item)
        await session.flush()

        result = await apply_accounts_delta(session, run_id=run.id)
        await session.commit()

    assert result.applied_count == 1
    assert result.failed_count == 0

    async with session_factory() as session:
        row = await session.execute(sa.select(Account).where(Account.id == account_id))
        account = row.scalar_one()
        assert account.display_name == 'New Name'
        assert account.mfa_enabled is True


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_account_revoke_disables_account(session_factory):
    """REVOKE delta → Account.status='disabled'."""
    async with session_factory() as session:
        app_id = await _make_app(session)
        account = Account(
            application_id=app_id,
            username='active.user',
            status=AccountStatus.active,
        )
        session.add(account)
        await session.flush()
        account_id = account.id

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.account)
        item = _account_item(
            run.id,
            ReconciliationDeltaOperation.revoke,
            entity_id=account_id,
            before_json={'status': 'active'},
        )
        session.add(item)
        await session.flush()

        result = await apply_accounts_delta(session, run_id=run.id)
        await session.commit()

    assert result.applied_count == 1

    async with session_factory() as session:
        row = await session.execute(sa.select(Account).where(Account.id == account_id))
        account = row.scalar_one()
        assert account.status == AccountStatus.disabled


# ---------------------------------------------------------------------------
# reactivate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_account_reactivate_enables_account(session_factory):
    """REACTIVATE delta → Account.status='active'."""
    async with session_factory() as session:
        app_id = await _make_app(session)
        account = Account(
            application_id=app_id,
            username='suspended.user',
            status=AccountStatus.disabled,
        )
        session.add(account)
        await session.flush()
        account_id = account.id

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.account)
        item = _account_item(
            run.id,
            ReconciliationDeltaOperation.reactivate,
            entity_id=account_id,
            before_json={'status': 'disabled'},
        )
        session.add(item)
        await session.flush()

        result = await apply_accounts_delta(session, run_id=run.id)
        await session.commit()

    assert result.applied_count == 1

    async with session_factory() as session:
        row = await session.execute(sa.select(Account).where(Account.id == account_id))
        account = row.scalar_one()
        assert account.status == AccountStatus.active


# ---------------------------------------------------------------------------
# apply_master_data_delta dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_master_data_delta_dispatches_to_account(session_factory):
    """apply_master_data_delta routes account entity_type to apply_accounts_delta."""
    async with session_factory() as session:
        app_id = await _make_app(session)
        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.account)
        # Advance to pending_apply so apply_master_data_delta accepts it
        run.status = ReconciliationRunStatus.pending_apply
        await session.flush()

        item = _account_item(
            run.id,
            ReconciliationDeltaOperation.create,
            after_json={
                'application_id': str(app_id),
                'username': 'dispatched.user@company.com',
                'status': 'active',
            },
        )
        session.add(item)
        await session.flush()

        result = await apply_master_data_delta(
            session,
            run_id=run.id,
            entity_type=ReconciliationEntityType.account,
        )
        await session.commit()

    assert isinstance(result, MasterDataApplyResult)
    assert result.applied_count == 1


# ---------------------------------------------------------------------------
# noop items are ignored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_account_noop_is_ignored(session_factory):
    """NOOP delta items are marked ignored, not counted as applied."""
    async with session_factory() as session:
        app_id = await _make_app(session)
        account = Account(
            application_id=app_id,
            username='unchanged.user',
            status=AccountStatus.active,
        )
        session.add(account)
        await session.flush()

        run = await create_run(session, application_id=None, entity_type=ReconciliationEntityType.account)
        item = _account_item(
            run.id,
            ReconciliationDeltaOperation.noop,
            entity_id=account.id,
        )
        session.add(item)
        await session.flush()

        result = await apply_accounts_delta(session, run_id=run.id)
        await session.commit()

    assert result.applied_count == 0
    assert result.ignored_count == 1

    async with session_factory() as session:
        row = await session.execute(sa.select(ReconciliationDeltaItem).where(ReconciliationDeltaItem.id == item.id))
        updated_item = row.scalar_one()
        assert updated_item.status == ReconciliationDeltaItemStatus.ignored

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for inventory_reconcile.handlers.account.AccountHandler.compute_delta."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
import uuid

from src.engines.inventory_reconcile.handlers.account import AccountHandler, ObservedAccount
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
)
from src.inventory.accounts.models import AccountStatus

_NOW = datetime(2026, 5, 12, 10, 0, 0, tzinfo=UTC)
_RUN_ID = uuid.uuid4()
_APP_ID = str(uuid.uuid4())


def _observed(**kwargs) -> ObservedAccount:
    defaults = {
        'application_id': _APP_ID,
        'username': 'test.user',
        'email': 'test.user@company.com',
        'display_name': 'Test User',
        'status': 'active',
        'is_privileged': False,
        'mfa_enabled': True,
        'observed_at': _NOW,
    }
    defaults.update(kwargs)
    return ObservedAccount(**defaults)


def _existing_account(**kwargs) -> MagicMock:
    """Return a mock Account-like object with sensible defaults."""
    account = MagicMock()
    account.id = uuid.uuid4()
    account.status = AccountStatus.active
    account.email = 'test.user@company.com'
    account.display_name = 'Test User'
    account.is_privileged = False
    account.mfa_enabled = True
    account.application_id = uuid.UUID(_APP_ID)
    account.username = 'test.user'
    for k, v in kwargs.items():
        setattr(account, k, v)
    return account


handler = AccountHandler()


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_compute_delta_create_when_existing_is_none():
    observed = _observed()
    item = handler.compute_delta(
        reconciliation_run_id=_RUN_ID,
        observed=observed,
        existing=None,
    )
    assert item.operation == ReconciliationDeltaOperation.create
    assert item.entity_type == ReconciliationEntityType.account
    assert item.entity_id is None
    assert item.before_json is None
    assert item.after_json is not None
    assert item.after_json['username'] == 'test.user'


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_compute_delta_update_on_field_change():
    existing = _existing_account(display_name='Old Name')
    observed = _observed(display_name='New Name')
    item = handler.compute_delta(
        reconciliation_run_id=_RUN_ID,
        observed=observed,
        existing=existing,
    )
    assert item.operation == ReconciliationDeltaOperation.update
    assert item.entity_id == existing.id
    assert item.before_json == {'display_name': 'Old Name'}
    assert item.after_json == {'display_name': 'New Name'}


def test_compute_delta_update_mfa_changed():
    existing = _existing_account(mfa_enabled=True)
    observed = _observed(mfa_enabled=False)
    item = handler.compute_delta(
        reconciliation_run_id=_RUN_ID,
        observed=observed,
        existing=existing,
    )
    assert item.operation == ReconciliationDeltaOperation.update
    assert 'mfa_enabled' in (item.before_json or {})


# ---------------------------------------------------------------------------
# revoke
# ---------------------------------------------------------------------------


def test_compute_delta_revoke_active_to_disabled():
    existing = _existing_account(status=AccountStatus.active)
    observed = _observed(status='disabled')
    item = handler.compute_delta(
        reconciliation_run_id=_RUN_ID,
        observed=observed,
        existing=existing,
    )
    assert item.operation == ReconciliationDeltaOperation.revoke
    assert item.entity_id == existing.id
    assert item.before_json is not None
    assert item.after_json is not None


# ---------------------------------------------------------------------------
# reactivate
# ---------------------------------------------------------------------------


def test_compute_delta_reactivate_disabled_to_active():
    existing = _existing_account(status=AccountStatus.disabled)
    observed = _observed(status='active')
    item = handler.compute_delta(
        reconciliation_run_id=_RUN_ID,
        observed=observed,
        existing=existing,
    )
    assert item.operation == ReconciliationDeltaOperation.reactivate
    assert item.entity_id == existing.id


# ---------------------------------------------------------------------------
# noop
# ---------------------------------------------------------------------------


def test_compute_delta_noop_when_nothing_changed():
    existing = _existing_account()
    observed = _observed()
    item = handler.compute_delta(
        reconciliation_run_id=_RUN_ID,
        observed=observed,
        existing=existing,
    )
    assert item.operation == ReconciliationDeltaOperation.noop
    assert item.entity_id == existing.id
    assert item.before_json is None
    assert item.after_json is None


# ---------------------------------------------------------------------------
# status-change takes priority over field diff
# ---------------------------------------------------------------------------


def test_revoke_takes_priority_over_field_changes():
    """revoke wins even if other fields also changed."""
    existing = _existing_account(status=AccountStatus.active, display_name='Old Name')
    observed = _observed(status='disabled', display_name='New Name')
    item = handler.compute_delta(
        reconciliation_run_id=_RUN_ID,
        observed=observed,
        existing=existing,
    )
    # Status change → revoke, not update
    assert item.operation == ReconciliationDeltaOperation.revoke

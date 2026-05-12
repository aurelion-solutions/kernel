# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Handler for entity_type='account'.

Computes a ReconciliationDeltaItem for one observed account vs. the existing
Account row in the PG ``ent_accounts`` table.

This handler is *not* invoked from the access-artifact normalization pipeline
(that flow is access_fact only).  Instead it is called directly from the
account reconciliation pipeline which supplies ObservedAccount objects.

Operations:
  create      — no Account row exists in PG yet
  update      — Account exists and at least one tracked field changed
  revoke      — existing.status=active  AND observed.status=disabled
  reactivate  — existing.status=disabled AND observed.status=active
  noop        — nothing changed
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
)
from src.inventory.accounts.models import Account, AccountStatus

# Fields compared field-by-field for update detection.
_TRACKED_FIELDS: tuple[str, ...] = (
    'display_name',
    'email',
    'is_privileged',
    'mfa_enabled',
)


class ObservedAccount(BaseModel):
    """Observed account data from a target system (connector payload)."""

    application_id: str
    username: str
    external_id: str | None = None
    email: str | None = None
    display_name: str | None = None
    status: str = 'active'
    is_privileged: bool = False
    mfa_enabled: bool = False
    meta: dict[str, Any] = {}
    observed_at: datetime


def _to_dict(observed: ObservedAccount) -> dict[str, Any]:
    """Serialize observed account to a flat dict for before/after_json."""
    return {
        'application_id': observed.application_id,
        'username': observed.username,
        'external_id': observed.external_id,
        'email': observed.email,
        'display_name': observed.display_name,
        'status': observed.status,
        'is_privileged': observed.is_privileged,
        'mfa_enabled': observed.mfa_enabled,
        'observed_at': observed.observed_at.isoformat(),
    }


def _account_to_dict(account: Account) -> dict[str, Any]:
    """Serialize an Account ORM row to a flat dict (before snapshot)."""
    return {
        'application_id': str(account.application_id),
        'username': account.username,
        'email': account.email,
        'display_name': account.display_name,
        'status': account.status.value if hasattr(account.status, 'value') else str(account.status),
        'is_privileged': account.is_privileged,
        'mfa_enabled': account.mfa_enabled,
    }


def _diff(existing: Account, observed: ObservedAccount) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (before, after) dicts for changed tracked fields only."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    mapping: dict[str, Any] = {
        'display_name': observed.display_name,
        'email': observed.email,
        'is_privileged': observed.is_privileged,
        'mfa_enabled': observed.mfa_enabled,
    }
    for field in _TRACKED_FIELDS:
        existing_val = getattr(existing, field)
        observed_val = mapping[field]
        if existing_val != observed_val:
            before[field] = existing_val
            after[field] = observed_val
    return before, after


class AccountHandler:
    """Compute a ReconciliationDeltaItem for one observed vs. existing account."""

    def compute_delta(
        self,
        *,
        reconciliation_run_id: Any,
        observed: ObservedAccount,
        existing: Account | None,
    ) -> ReconciliationDeltaItem:
        """Return a delta item describing what changed (or noop if nothing did)."""
        if existing is None:
            return ReconciliationDeltaItem(
                reconciliation_run_id=reconciliation_run_id,
                entity_type=ReconciliationEntityType.account,
                operation=ReconciliationDeltaOperation.create,
                entity_id=None,
                before_json=None,
                after_json=_to_dict(observed),
                status=ReconciliationDeltaItemStatus.pending,
            )

        existing_status_val = existing.status.value if hasattr(existing.status, 'value') else str(existing.status)
        observed_status = observed.status.lower()

        # Status transitions take priority over field-level diff.
        if existing_status_val == AccountStatus.active and observed_status == 'disabled':
            return ReconciliationDeltaItem(
                reconciliation_run_id=reconciliation_run_id,
                entity_type=ReconciliationEntityType.account,
                operation=ReconciliationDeltaOperation.revoke,
                entity_id=existing.id,
                before_json=_account_to_dict(existing),
                after_json=_to_dict(observed),
                status=ReconciliationDeltaItemStatus.pending,
            )

        if existing_status_val == 'disabled' and observed_status == AccountStatus.active:
            return ReconciliationDeltaItem(
                reconciliation_run_id=reconciliation_run_id,
                entity_type=ReconciliationEntityType.account,
                operation=ReconciliationDeltaOperation.reactivate,
                entity_id=existing.id,
                before_json=_account_to_dict(existing),
                after_json=_to_dict(observed),
                status=ReconciliationDeltaItemStatus.pending,
            )

        before, after = _diff(existing, observed)
        if before:
            return ReconciliationDeltaItem(
                reconciliation_run_id=reconciliation_run_id,
                entity_type=ReconciliationEntityType.account,
                operation=ReconciliationDeltaOperation.update,
                entity_id=existing.id,
                before_json=before,
                after_json=after,
                status=ReconciliationDeltaItemStatus.pending,
            )

        return ReconciliationDeltaItem(
            reconciliation_run_id=reconciliation_run_id,
            entity_type=ReconciliationEntityType.account,
            operation=ReconciliationDeltaOperation.noop,
            entity_id=existing.id,
            before_json=None,
            after_json=None,
            status=ReconciliationDeltaItemStatus.pending,
        )


# AccountHandler is not an artifact normalization handler (Handler Protocol).
# It is an entity-level reconciliation handler with a compute_delta interface.
# It does NOT register in the artifact handler registry.
_default_handler = AccountHandler()

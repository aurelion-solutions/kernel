# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for D3 account-status resolver.

Covers all documented transitions:
  add_fact path:
    not_exists → account_create
    not_exists → account_invite (create not available)
    not_exists → None (no path to active)
    invited → account_activate
    invited → None (transition unavailable)
    suspended → account_activate
    disabled → account_activate
    active → None (already active, no op)
    deleted/unknown → None

  remove_fact path:
    active → account_suspend (prefer over disable)
    active → account_disable (only disable available)
    active → None (no transitions)
    invited → account_disable
    invited → None (no disable transition)
    suspended → None (already non-active)
    disabled → None
    not_exists → None

  Non-account fact kinds:
    add_fact role  → grant_role
    add_fact group → group_add
    add_fact entitlement → entitlement_attach
    add_fact unknown_kind → grant_role (default)
    remove_fact role  → revoke_role
    remove_fact group → group_remove
    remove_fact entitlement → entitlement_detach

  resolve_item_kind dispatch:
    is_account_item=True delegates to account resolver
    is_account_item=False delegates to fact resolver
    account resolver returns None → falls back to fact op
"""

from __future__ import annotations

import pytest
from src.engines.access_plan.models import PlanItemKind
from src.engines.access_plan.status_resolver import (
    ABSTRACT_ADD,
    ABSTRACT_REMOVE,
    NOT_EXISTS,
    resolve_account_op_kind,
    resolve_fact_op_kind,
    resolve_item_kind,
)
from src.inventory.accounts.models import AccountStatus
from src.platform.connectors.registration_schemas import AccountStatusTransitions

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _transitions(*pairs: tuple[str, str]) -> AccountStatusTransitions:
    """Build AccountStatusTransitions from (from, to) pairs."""
    return AccountStatusTransitions(transitions=list(pairs))


# ---------------------------------------------------------------------------
# add_fact — account resolver
# ---------------------------------------------------------------------------


class TestAddFactAccountOp:
    def test_not_exists_create_preferred_over_invite(self) -> None:
        trans = _transitions(
            (NOT_EXISTS, AccountStatus.active),
            (NOT_EXISTS, AccountStatus.invited),
        )
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_ADD,
            current_status=NOT_EXISTS,
            transitions=trans,
        )
        assert result == PlanItemKind.account_create

    def test_not_exists_invite_when_create_unavailable(self) -> None:
        trans = _transitions((NOT_EXISTS, AccountStatus.invited))
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_ADD,
            current_status=NOT_EXISTS,
            transitions=trans,
        )
        assert result == PlanItemKind.account_invite

    def test_not_exists_no_path_returns_none(self) -> None:
        trans = _transitions((AccountStatus.active, AccountStatus.suspended))
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_ADD,
            current_status=NOT_EXISTS,
            transitions=trans,
        )
        assert result is None

    def test_invited_activates(self) -> None:
        trans = _transitions((AccountStatus.invited, AccountStatus.active))
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_ADD,
            current_status=AccountStatus.invited,
            transitions=trans,
        )
        assert result == PlanItemKind.account_activate

    def test_invited_no_transition_returns_none(self) -> None:
        trans = _transitions((NOT_EXISTS, AccountStatus.active))
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_ADD,
            current_status=AccountStatus.invited,
            transitions=trans,
        )
        assert result is None

    def test_suspended_activates(self) -> None:
        trans = _transitions(
            (NOT_EXISTS, AccountStatus.active),
            (AccountStatus.suspended, AccountStatus.active),
        )
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_ADD,
            current_status=AccountStatus.suspended,
            transitions=trans,
        )
        assert result == PlanItemKind.account_activate

    def test_disabled_activates(self) -> None:
        trans = _transitions((AccountStatus.disabled, AccountStatus.active))
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_ADD,
            current_status=AccountStatus.disabled,
            transitions=trans,
        )
        assert result == PlanItemKind.account_activate

    def test_already_active_no_op(self) -> None:
        trans = _transitions(
            (NOT_EXISTS, AccountStatus.active),
            (AccountStatus.suspended, AccountStatus.active),
        )
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_ADD,
            current_status=AccountStatus.active,
            transitions=trans,
        )
        assert result is None

    @pytest.mark.parametrize('status', [AccountStatus.deleted, AccountStatus.unknown])
    def test_terminal_or_unknown_statuses_return_none(self, status: str) -> None:
        trans = _transitions(
            (NOT_EXISTS, AccountStatus.active),
            (AccountStatus.suspended, AccountStatus.active),
        )
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_ADD,
            current_status=status,
            transitions=trans,
        )
        assert result is None


# ---------------------------------------------------------------------------
# remove_fact — account resolver
# ---------------------------------------------------------------------------


class TestRemoveFactAccountOp:
    def test_active_suspend_preferred_over_disable(self) -> None:
        trans = _transitions(
            (AccountStatus.active, AccountStatus.suspended),
            (AccountStatus.active, AccountStatus.disabled),
        )
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_REMOVE,
            current_status=AccountStatus.active,
            transitions=trans,
        )
        assert result == PlanItemKind.account_suspend

    def test_active_disable_when_no_suspend(self) -> None:
        trans = _transitions((AccountStatus.active, AccountStatus.disabled))
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_REMOVE,
            current_status=AccountStatus.active,
            transitions=trans,
        )
        assert result == PlanItemKind.account_disable

    def test_active_no_transition_returns_none(self) -> None:
        trans = _transitions((NOT_EXISTS, AccountStatus.active))
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_REMOVE,
            current_status=AccountStatus.active,
            transitions=trans,
        )
        assert result is None

    def test_invited_disable(self) -> None:
        trans = _transitions((AccountStatus.invited, AccountStatus.disabled))
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_REMOVE,
            current_status=AccountStatus.invited,
            transitions=trans,
        )
        assert result == PlanItemKind.account_disable

    def test_invited_no_disable_returns_none(self) -> None:
        trans = _transitions((AccountStatus.invited, AccountStatus.active))
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_REMOVE,
            current_status=AccountStatus.invited,
            transitions=trans,
        )
        assert result is None

    @pytest.mark.parametrize(
        'status',
        [AccountStatus.suspended, AccountStatus.disabled, NOT_EXISTS],
    )
    def test_non_active_statuses_return_none(self, status: str) -> None:
        trans = _transitions(
            (AccountStatus.active, AccountStatus.suspended),
            (AccountStatus.active, AccountStatus.disabled),
        )
        result = resolve_account_op_kind(
            abstract_kind=ABSTRACT_REMOVE,
            current_status=status,
            transitions=trans,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Non-account fact kinds
# ---------------------------------------------------------------------------


class TestFactOpKind:
    @pytest.mark.parametrize(
        ('fact_kind', 'expected'),
        [
            ('role', PlanItemKind.grant_role),
            ('group', PlanItemKind.group_add),
            ('entitlement', PlanItemKind.entitlement_attach),
            ('unknown_kind', PlanItemKind.grant_role),  # default
        ],
    )
    def test_add_fact(self, fact_kind: str, expected: PlanItemKind) -> None:
        result = resolve_fact_op_kind(
            abstract_kind=ABSTRACT_ADD,
            target_descriptor={'fact_kind': fact_kind},
        )
        assert result == expected

    @pytest.mark.parametrize(
        ('fact_kind', 'expected'),
        [
            ('role', PlanItemKind.revoke_role),
            ('group', PlanItemKind.group_remove),
            ('entitlement', PlanItemKind.entitlement_detach),
            ('unknown_kind', PlanItemKind.revoke_role),  # default
        ],
    )
    def test_remove_fact(self, fact_kind: str, expected: PlanItemKind) -> None:
        result = resolve_fact_op_kind(
            abstract_kind=ABSTRACT_REMOVE,
            target_descriptor={'fact_kind': fact_kind},
        )
        assert result == expected

    def test_missing_fact_kind_defaults_to_role(self) -> None:
        result = resolve_fact_op_kind(
            abstract_kind=ABSTRACT_ADD,
            target_descriptor={},
        )
        assert result == PlanItemKind.grant_role


# ---------------------------------------------------------------------------
# resolve_item_kind high-level dispatch
# ---------------------------------------------------------------------------


class TestResolveItemKind:
    def test_account_item_dispatches_to_account_resolver(self) -> None:
        trans = _transitions((NOT_EXISTS, AccountStatus.active))
        result = resolve_item_kind(
            abstract_kind=ABSTRACT_ADD,
            is_account_item=True,
            current_account_status=NOT_EXISTS,
            transitions=trans,
            target_descriptor={'fact_kind': 'account'},
        )
        assert result == PlanItemKind.account_create

    def test_non_account_item_dispatches_to_fact_resolver(self) -> None:
        result = resolve_item_kind(
            abstract_kind=ABSTRACT_ADD,
            is_account_item=False,
            current_account_status=None,
            transitions=None,
            target_descriptor={'fact_kind': 'group'},
        )
        assert result == PlanItemKind.group_add

    def test_account_resolver_none_falls_back_to_fact_op(self) -> None:
        # active status on add_fact → account op returns None → fallback
        trans = _transitions((NOT_EXISTS, AccountStatus.active))
        result = resolve_item_kind(
            abstract_kind=ABSTRACT_ADD,
            is_account_item=True,
            current_account_status=AccountStatus.active,
            transitions=trans,
            target_descriptor={'fact_kind': 'role'},
        )
        # Falls back to grant_role because account is already active
        assert result == PlanItemKind.grant_role

    def test_none_transitions_uses_empty_transitions(self) -> None:
        """None transitions → AccountStatusTransitions() with empty list → account op None → fallback."""
        result = resolve_item_kind(
            abstract_kind=ABSTRACT_ADD,
            is_account_item=True,
            current_account_status=NOT_EXISTS,
            transitions=None,
            target_descriptor={'fact_kind': 'role'},
        )
        # Empty transitions → no path → fallback to fact op grant_role
        assert result == PlanItemKind.grant_role

    def test_none_account_status_treated_as_not_exists(self) -> None:
        trans = _transitions((NOT_EXISTS, AccountStatus.active))
        result = resolve_item_kind(
            abstract_kind=ABSTRACT_ADD,
            is_account_item=True,
            current_account_status=None,
            transitions=trans,
            target_descriptor={'fact_kind': 'account'},
        )
        assert result == PlanItemKind.account_create

    def test_remove_fact_active_suspend(self) -> None:
        trans = _transitions((AccountStatus.active, AccountStatus.suspended))
        result = resolve_item_kind(
            abstract_kind=ABSTRACT_REMOVE,
            is_account_item=True,
            current_account_status=AccountStatus.active,
            transitions=trans,
            target_descriptor={'fact_kind': 'account'},
        )
        assert result == PlanItemKind.account_suspend

    def test_remove_fact_non_account_revoke_role(self) -> None:
        result = resolve_item_kind(
            abstract_kind=ABSTRACT_REMOVE,
            is_account_item=False,
            current_account_status=None,
            transitions=None,
            target_descriptor={'fact_kind': 'role'},
        )
        assert result == PlanItemKind.revoke_role

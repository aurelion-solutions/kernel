# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""D3 — Account-status reasoning: abstract diff → concrete operation kinds.

The resolver reads:
 - Current account.status from the accounts table for (application, subject).
   ``not_exists`` is the sentinel when no account row is found.
 - Connector capability descriptor (AccountStatusTransitions) from B1.

For each diff item it decides the correct PlanItemKind:

add_fact items
  ``not_exists`` → account_create  (if transition not_exists→active supported)
  ``not_exists`` → account_invite  (if transition not_exists→invited supported
                                     and NOT not_exists→active)
  ``invited``    → account_activate (if transition invited→active supported)
  ``suspended``  → account_activate (if transition suspended→active supported)
  ``disabled``   → account_activate (if transition disabled→active supported)
  ``active``     → skip account op (account already active; kind remains grant)
  Otherwise      → skip account op (best-effort; D4 will mark unsatisfiable)

remove_fact items (ensure account NOT active)
  ``active``     → account_suspend  (if transition active→suspended supported)
  ``active``     → account_disable  (if transition active→disabled supported,
                                      and NOT active→suspended)
  ``invited``    → account_disable  (connector can skip invite flow)
  non-active     → skip account op  (already in target state)

Grant / entitlement / group fact items (non-account facts) use target_descriptor
to pick the corresponding grant_role / revoke_role / group_add / group_remove /
entitlement_attach / entitlement_detach kind.  The choice is driven by the
abstract_kind stored in the diff item:
  add_fact  → grant_role / group_add / entitlement_attach  (by fact_kind)
  remove_fact → revoke_role / group_remove / entitlement_detach

Fact-kind vocabulary (stored in target_descriptor['fact_kind'] or fallback):
  'role'          → grant_role / revoke_role
  'group'         → group_add / group_remove
  'entitlement'   → entitlement_attach / entitlement_detach
  <anything else> → grant_role / revoke_role  (safe default)

The resolver is a **pure function** over inputs — no DB calls are made here.
DB access is centralised in repository.py; callers (service.py) pass already-
fetched account status and descriptor into ``resolve_item_kind``.
"""

from __future__ import annotations

from typing import Any

from src.engines.access_plan.models import PlanItemKind
from src.inventory.accounts.models import AccountStatus
from src.platform.connectors.registration_schemas import AccountStatusTransitions

# ---------------------------------------------------------------------------
# Sentinel for absent account row
# ---------------------------------------------------------------------------

NOT_EXISTS = 'not_exists'

# ---------------------------------------------------------------------------
# Transition set helpers
# ---------------------------------------------------------------------------


def _transition_set(transitions: AccountStatusTransitions) -> set[tuple[str, str]]:
    """Return the transition set as a Python set of (from, to) tuples."""
    return {(f, t) for f, t in transitions.transitions}


def _has(tset: set[tuple[str, str]], from_status: str, to_status: str) -> bool:
    return (from_status, to_status) in tset


# ---------------------------------------------------------------------------
# Core resolver
# ---------------------------------------------------------------------------

# Abstract change kind markers (mirrors service.py constants)
ABSTRACT_ADD = 'add_fact'
ABSTRACT_REMOVE = 'remove_fact'

# Fact-kind → add/remove kind mapping
_FACT_KIND_ADD: dict[str, PlanItemKind] = {
    'role': PlanItemKind.grant_role,
    'group': PlanItemKind.group_add,
    'entitlement': PlanItemKind.entitlement_attach,
}
_FACT_KIND_REMOVE: dict[str, PlanItemKind] = {
    'role': PlanItemKind.revoke_role,
    'group': PlanItemKind.group_remove,
    'entitlement': PlanItemKind.entitlement_detach,
}


def _fact_kind_from_descriptor(target_descriptor: dict[str, Any]) -> str:
    """Extract fact_kind from target_descriptor; default to 'role'."""
    return str(target_descriptor.get('fact_kind', 'role'))


def resolve_account_op_kind(
    *,
    abstract_kind: str,
    current_status: str,
    transitions: AccountStatusTransitions,
) -> PlanItemKind | None:
    """Resolve the account-lifecycle PlanItemKind for an account-level diff item.

    Returns None when no account operation is needed (account already in the
    desired state) or when no supported transition is available.

    Args:
        abstract_kind: 'add_fact' or 'remove_fact'.
        current_status: Current ``AccountStatus`` value or ``NOT_EXISTS`` sentinel.
        transitions: Connector-supplied allowed transitions.

    Returns:
        The concrete ``PlanItemKind`` for the required account operation,
        or ``None`` if no operation is needed.
    """
    tset = _transition_set(transitions)

    if abstract_kind == ABSTRACT_ADD:
        return _resolve_add_account_op(current_status, tset)
    if abstract_kind == ABSTRACT_REMOVE:
        return _resolve_remove_account_op(current_status, tset)
    return None


def _resolve_add_account_op(
    current_status: str,
    tset: set[tuple[str, str]],
) -> PlanItemKind | None:
    """Return the op needed to bring the account to 'active'."""
    if current_status == NOT_EXISTS:
        # Prefer create over invite; invite is the fallback when create unsupported
        if _has(tset, NOT_EXISTS, AccountStatus.active):
            return PlanItemKind.account_create
        if _has(tset, NOT_EXISTS, AccountStatus.invited):
            return PlanItemKind.account_invite
        # No path to active — caller will mark unsatisfiable in D4
        return None

    if current_status == AccountStatus.invited:
        if _has(tset, AccountStatus.invited, AccountStatus.active):
            return PlanItemKind.account_activate
        return None

    if current_status in (AccountStatus.suspended, AccountStatus.disabled):
        if _has(tset, current_status, AccountStatus.active):
            return PlanItemKind.account_activate
        return None

    if current_status == AccountStatus.active:
        # Already active — no op needed
        return None

    # deleted / unknown / anything else: no known transition
    return None


def _resolve_remove_account_op(
    current_status: str,
    tset: set[tuple[str, str]],
) -> PlanItemKind | None:
    """Return the op needed to deactivate the account."""
    if current_status == AccountStatus.active:
        # Prefer suspend over disable
        if _has(tset, AccountStatus.active, AccountStatus.suspended):
            return PlanItemKind.account_suspend
        if _has(tset, AccountStatus.active, AccountStatus.disabled):
            return PlanItemKind.account_disable
        return None

    if current_status == AccountStatus.invited:
        if _has(tset, AccountStatus.invited, AccountStatus.disabled):
            return PlanItemKind.account_disable
        return None

    # suspended, disabled, deleted, not_exists: already non-active
    return None


# ---------------------------------------------------------------------------
# Non-account fact-kind resolver
# ---------------------------------------------------------------------------


def resolve_fact_op_kind(
    *,
    abstract_kind: str,
    target_descriptor: dict[str, Any],
) -> PlanItemKind:
    """Return the concrete PlanItemKind for a non-account fact diff item.

    Uses ``target_descriptor['fact_kind']`` to distinguish role / group /
    entitlement.  Defaults to 'role' when the key is absent.
    """
    fk = _fact_kind_from_descriptor(target_descriptor)
    if abstract_kind == ABSTRACT_ADD:
        return _FACT_KIND_ADD.get(fk, PlanItemKind.grant_role)
    return _FACT_KIND_REMOVE.get(fk, PlanItemKind.revoke_role)


# ---------------------------------------------------------------------------
# High-level item resolver
# ---------------------------------------------------------------------------


def resolve_item_kind(
    *,
    abstract_kind: str,
    is_account_item: bool,
    current_account_status: str | None,
    transitions: AccountStatusTransitions | None,
    target_descriptor: dict[str, Any],
) -> PlanItemKind:
    """Resolve the concrete PlanItemKind for a diff item.

    Dispatches to the account-op resolver when ``is_account_item=True``,
    otherwise to the fact-op resolver.

    When the account-op resolver returns ``None`` (no op needed or transition
    unavailable) this function falls back to the fact-op kind — the D4 DAG
    resolver is responsible for marking unsatisfiable items.

    Args:
        abstract_kind: 'add_fact' or 'remove_fact'.
        is_account_item: True when the diff item represents an account-level
            change (target_descriptor contains ``{'fact_kind': 'account'}`` or
            the caller explicitly set this flag).
        current_account_status: Current ``AccountStatus.value`` or
            ``NOT_EXISTS``, required when ``is_account_item=True``.
        transitions: Connector ``AccountStatusTransitions``, required when
            ``is_account_item=True``.
        target_descriptor: Descriptor dict from the diff item.

    Returns:
        Concrete ``PlanItemKind``.
    """
    if is_account_item:
        status = current_account_status or NOT_EXISTS
        trans = transitions or AccountStatusTransitions()
        op = resolve_account_op_kind(
            abstract_kind=abstract_kind,
            current_status=status,
            transitions=trans,
        )
        if op is not None:
            return op
        # Fallback: no account op; treat as grant/revoke
        return resolve_fact_op_kind(
            abstract_kind=abstract_kind,
            target_descriptor=target_descriptor,
        )
    return resolve_fact_op_kind(
        abstract_kind=abstract_kind,
        target_descriptor=target_descriptor,
    )

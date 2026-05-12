# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Second in-process mock connector for Phase 19 testing (Step G2).

This is the **second reference connector** — a connector with a richer data
model to prove that the declarative diff mechanism is not tied to any specific
shape of data.  Three distinguishing characteristics vs the G1 flat connector:

1. **Hierarchical groups** — groups can contain other groups (transitive
   membership).  ``group_add`` / ``group_remove`` accept ``member_ref`` which
   is either an ``account_ref`` (leaf) or a ``group_ref`` (nested).  The helper
   ``resolve_members`` returns the full transitive member set.

2. **Conditional grants** — ``grant_role`` checks a *security clearance level*
   attribute on the subject before applying.  The operation descriptor encodes
   this as a ``dependency_rules`` entry with ``resource='subject_attribute'``
   and ``status=['clearance:secret', 'clearance:top_secret']``.  The handler
   enforces it at apply time.

3. **Explicit ``invited → active`` transitions** — ``account_invite`` creates
   an ``invited`` account, ``account_activate`` transitions it to ``active``.
   The account_status transitions encode the full graph including a
   ``suspended → disabled`` path (absent in G1, present here).

Public surface
--------------
- ``HIERARCHICAL_CONNECTOR_DESCRIPTOR`` — ``ConnectorCapabilityDescriptor``
  for registration.
- ``HIERARCHICAL_CONNECTOR_SUPPORTED_OPERATIONS`` — list of all 11 operation
  kind strings.
- ``HierarchicalConnectorState`` — in-memory state with nested group support.
- ``HierarchicalConnectorHandler`` — handler with conditional-grant enforcement
  and transitive group membership resolution.
- ``VerifyFactResult`` re-exported from ``mock_connector`` for convenience.

Design contract
---------------
- No I/O, no DB, no network.
- Thread-safe: asyncio.Lock guards all state mutations.
- Cycles in the group graph are detected during ``resolve_members`` to prevent
  infinite recursion.
- ``force_timeout=True`` makes every ``verify_fact`` return ``timeout``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from src.platform.connectors.mock_connector import VerifyFactResult
from src.platform.connectors.registration_schemas import (
    AccountStatusTransitions,
    ConnectorCapabilityDescriptor,
    ConnectorOperationDescriptor,
    OperationDependencyRule,
)

# ---------------------------------------------------------------------------
# Supported operations (all 11 from Exit Criterion)
# ---------------------------------------------------------------------------

HIERARCHICAL_CONNECTOR_SUPPORTED_OPERATIONS: list[str] = [
    'account_create',
    'account_invite',
    'account_activate',
    'account_suspend',
    'account_disable',
    'grant_role',
    'revoke_role',
    'group_add',
    'group_remove',
    'entitlement_attach',
    'entitlement_detach',
]

# ---------------------------------------------------------------------------
# Capability descriptor
# ---------------------------------------------------------------------------

HIERARCHICAL_CONNECTOR_DESCRIPTOR = ConnectorCapabilityDescriptor(
    operations=[
        # Account lifecycle — no prerequisites (invite flow is explicit here)
        ConnectorOperationDescriptor(kind='account_create', dependency_rules=[]),
        ConnectorOperationDescriptor(kind='account_invite', dependency_rules=[]),
        ConnectorOperationDescriptor(kind='account_activate', dependency_rules=[]),
        ConnectorOperationDescriptor(kind='account_suspend', dependency_rules=[]),
        ConnectorOperationDescriptor(kind='account_disable', dependency_rules=[]),
        # Role operations — require active account + sufficient clearance level
        ConnectorOperationDescriptor(
            kind='grant_role',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active']),
                OperationDependencyRule(
                    resource='subject_attribute',
                    status=['clearance:secret', 'clearance:top_secret'],
                ),
            ],
        ),
        ConnectorOperationDescriptor(
            kind='revoke_role',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active', 'suspended']),
            ],
        ),
        # Group operations — accept account OR group as member (hierarchical)
        ConnectorOperationDescriptor(
            kind='group_add',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active', 'invited']),
            ],
        ),
        ConnectorOperationDescriptor(
            kind='group_remove',
            dependency_rules=[],
        ),
        # Entitlement operations — require active account
        ConnectorOperationDescriptor(
            kind='entitlement_attach',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active']),
            ],
        ),
        ConnectorOperationDescriptor(
            kind='entitlement_detach',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active', 'suspended']),
            ],
        ),
    ],
    # Richer transition graph than G1: adds suspended -> disabled
    account_status=AccountStatusTransitions(
        transitions=[
            ('not_exists', 'invited'),
            ('invited', 'active'),
            ('active', 'suspended'),
            ('suspended', 'active'),
            ('active', 'disabled'),
            ('suspended', 'disabled'),
        ]
    ),
    verify_fact_supported=True,
    supported_fact_kinds=['account', 'role', 'group', 'entitlement'],
)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------


@dataclass
class HierarchicalConnectorState:
    """In-memory state with hierarchical group support.

    Groups
    ------
    A group may contain *accounts* (leaf members) or other *groups* (nested).
    Both are tracked in ``group_members``:

        group_members[group_ref] = set of (account_ref | group_ref)

    This makes the graph a DAG (cycle detection is enforced at add time).

    Subject attributes
    ------------------
    ``subject_attributes[account_ref]`` holds a set of string attribute tokens,
    e.g. ``{'clearance:secret', 'dept:engineering'}``.  These are checked by
    ``grant_role`` enforcement.
    """

    # account_ref -> status
    accounts: dict[str, str] = field(default_factory=dict)

    # account_ref -> set of role_refs
    roles: dict[str, set[str]] = field(default_factory=dict)

    # group_ref -> set of (account_ref | group_ref) — direct members only
    group_members: dict[str, set[str]] = field(default_factory=dict)

    # account_ref -> set of entitlement_refs
    entitlements: dict[str, set[str]] = field(default_factory=dict)

    # account_ref -> set of attribute tokens
    subject_attributes: dict[str, set[str]] = field(default_factory=dict)

    def account_status(self, account_ref: str) -> str:
        """Return account status or ``not_exists`` when absent."""
        return self.accounts.get(account_ref, 'not_exists')

    def has_role(self, account_ref: str, role_ref: str) -> bool:
        return role_ref in self.roles.get(account_ref, set())

    def has_entitlement(self, account_ref: str, entitlement_ref: str) -> bool:
        return entitlement_ref in self.entitlements.get(account_ref, set())

    def has_attribute(self, account_ref: str, attribute: str) -> bool:
        return attribute in self.subject_attributes.get(account_ref, set())

    def resolve_members(self, group_ref: str, *, _visited: frozenset[str] | None = None) -> set[str]:
        """Return the full transitive set of *account* members for a group.

        Only leaf members (account refs) are returned.  Nested group refs are
        expanded recursively.  Cycle-safe via ``_visited`` guard.
        """
        visited = _visited if _visited is not None else frozenset()
        if group_ref in visited:
            # Cycle detected — return empty to avoid infinite recursion
            return set()
        visited = visited | {group_ref}

        result: set[str] = set()
        for member in self.group_members.get(group_ref, set()):
            if member in self.accounts:
                # Leaf member (account)
                result.add(member)
            elif member in self.group_members:
                # Nested group — recurse
                result |= self.resolve_members(member, _visited=visited)
        return result

    def is_member(self, group_ref: str, account_ref: str) -> bool:
        """Return True if ``account_ref`` is a transitive member of ``group_ref``."""
        return account_ref in self.resolve_members(group_ref)

    def _would_create_cycle(self, group_ref: str, new_member: str) -> bool:
        """Return True if adding ``new_member`` to ``group_ref`` would create a cycle."""
        # A cycle exists if group_ref is reachable from new_member
        if new_member not in self.group_members:
            return False
        return group_ref in self._reachable_groups(new_member, visited=frozenset())

    def _reachable_groups(self, start: str, *, visited: frozenset[str]) -> set[str]:
        if start in visited:
            return set()
        visited = visited | {start}
        reachable: set[str] = {start}
        for member in self.group_members.get(start, set()):
            if member in self.group_members:
                reachable |= self._reachable_groups(member, visited=visited)
        return reachable


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HierarchicalConnectorHandler:
    """Processes operation payloads against :class:`HierarchicalConnectorState`.

    Thread-safety: asyncio.Lock guards all state mutations.

    Parameters
    ----------
    state:
        Shared in-memory state.  Defaults to a fresh empty state.
    force_timeout:
        When ``True``, every ``verify_fact`` call returns ``timeout``.
    """

    def __init__(
        self,
        *,
        state: HierarchicalConnectorState | None = None,
        force_timeout: bool = False,
    ) -> None:
        self._state = state if state is not None else HierarchicalConnectorState()
        self._force_timeout = force_timeout
        self._lock = asyncio.Lock()

    @property
    def state(self) -> HierarchicalConnectorState:
        return self._state

    # ------------------------------------------------------------------
    # Account lifecycle
    # ------------------------------------------------------------------

    async def account_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create account with status ``active``."""
        account_ref = _require(payload, 'account_ref')
        async with self._lock:
            self._state.accounts[account_ref] = 'active'
        return {'status': 'ok', 'account_ref': account_ref, 'account_status': 'active'}

    async def account_invite(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create account with status ``invited`` (explicit invite flow)."""
        account_ref = _require(payload, 'account_ref')
        async with self._lock:
            self._state.accounts[account_ref] = 'invited'
        return {'status': 'ok', 'account_ref': account_ref, 'account_status': 'invited'}

    async def account_activate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Transition ``invited`` account to ``active``."""
        account_ref = _require(payload, 'account_ref')
        async with self._lock:
            current = self._state.account_status(account_ref)
            if current not in ('invited', 'suspended'):
                return {
                    'status': 'error',
                    'error': {'message': f'Cannot activate from status {current!r}'},
                }
            self._state.accounts[account_ref] = 'active'
        return {'status': 'ok', 'account_ref': account_ref, 'account_status': 'active'}

    async def account_suspend(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Transition ``active`` account to ``suspended``."""
        account_ref = _require(payload, 'account_ref')
        async with self._lock:
            current = self._state.account_status(account_ref)
            if current != 'active':
                return {
                    'status': 'error',
                    'error': {'message': f'Cannot suspend from status {current!r}'},
                }
            self._state.accounts[account_ref] = 'suspended'
        return {'status': 'ok', 'account_ref': account_ref, 'account_status': 'suspended'}

    async def account_disable(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Transition ``active`` or ``suspended`` account to ``disabled``."""
        account_ref = _require(payload, 'account_ref')
        async with self._lock:
            current = self._state.account_status(account_ref)
            if current not in ('active', 'suspended'):
                return {
                    'status': 'error',
                    'error': {'message': f'Cannot disable from status {current!r}'},
                }
            self._state.accounts[account_ref] = 'disabled'
        return {'status': 'ok', 'account_ref': account_ref, 'account_status': 'disabled'}

    # ------------------------------------------------------------------
    # Role operations (with clearance check)
    # ------------------------------------------------------------------

    async def grant_role(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Grant a role, enforcing clearance-level conditional dependency.

        Payload keys
        ------------
        account_ref: str
        role_ref: str
        subject_clearance: str | None
            Clearance token to check, e.g. ``'clearance:secret'``.
            If omitted the handler falls back to checking ``state.subject_attributes``.
        """
        account_ref = _require(payload, 'account_ref')
        role_ref = _require(payload, 'role_ref')

        # Resolve clearance: prefer explicit payload, fall back to state attributes
        clearance = payload.get('subject_clearance')
        async with self._lock:
            if clearance is None:
                attrs = self._state.subject_attributes.get(account_ref, set())
                clearance = next(
                    (a for a in attrs if a.startswith('clearance:')),
                    None,
                )
            if clearance not in ('clearance:secret', 'clearance:top_secret'):
                return {
                    'status': 'error',
                    'error': {
                        'message': (
                            f'Insufficient clearance for grant_role: got {clearance!r}, '
                            'required clearance:secret or clearance:top_secret'
                        )
                    },
                }
            self._state.roles.setdefault(account_ref, set()).add(role_ref)
        return {'status': 'ok', 'account_ref': account_ref, 'role_ref': role_ref}

    async def revoke_role(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Revoke a role from an account (no clearance check required)."""
        account_ref = _require(payload, 'account_ref')
        role_ref = _require(payload, 'role_ref')
        async with self._lock:
            self._state.roles.get(account_ref, set()).discard(role_ref)
        return {'status': 'ok', 'account_ref': account_ref, 'role_ref': role_ref}

    # ------------------------------------------------------------------
    # Group operations (hierarchical)
    # ------------------------------------------------------------------

    async def group_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add a member (account OR group) to a group.

        Payload keys
        ------------
        group_ref: str
            Target group.
        member_ref: str
            Account ref (leaf) or group ref (nested).

        Errors
        ------
        - Cycle detection: adding ``member_ref`` must not create a group cycle.
        """
        group_ref = _require(payload, 'group_ref')
        member_ref = _require(payload, 'member_ref')

        async with self._lock:
            # Cycle detection (only relevant when member_ref is itself a group)
            if member_ref in self._state.group_members and self._state._would_create_cycle(group_ref, member_ref):
                return {
                    'status': 'error',
                    'error': {'message': f'Adding {member_ref!r} to {group_ref!r} would create a cycle'},
                }
            self._state.group_members.setdefault(group_ref, set()).add(member_ref)
        return {'status': 'ok', 'group_ref': group_ref, 'member_ref': member_ref}

    async def group_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove a direct member from a group."""
        group_ref = _require(payload, 'group_ref')
        member_ref = _require(payload, 'member_ref')
        async with self._lock:
            self._state.group_members.get(group_ref, set()).discard(member_ref)
        return {'status': 'ok', 'group_ref': group_ref, 'member_ref': member_ref}

    # ------------------------------------------------------------------
    # Entitlement operations
    # ------------------------------------------------------------------

    async def entitlement_attach(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Attach an entitlement to an account."""
        account_ref = _require(payload, 'account_ref')
        entitlement_ref = _require(payload, 'entitlement_ref')
        async with self._lock:
            self._state.entitlements.setdefault(account_ref, set()).add(entitlement_ref)
        return {'status': 'ok', 'account_ref': account_ref, 'entitlement_ref': entitlement_ref}

    async def entitlement_detach(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Detach an entitlement from an account."""
        account_ref = _require(payload, 'account_ref')
        entitlement_ref = _require(payload, 'entitlement_ref')
        async with self._lock:
            self._state.entitlements.get(account_ref, set()).discard(entitlement_ref)
        return {'status': 'ok', 'account_ref': account_ref, 'entitlement_ref': entitlement_ref}

    # ------------------------------------------------------------------
    # verify_fact primitive
    # ------------------------------------------------------------------

    async def verify_fact(
        self,
        descriptor: dict[str, Any],
        expected_state: dict[str, Any],
    ) -> VerifyFactResult:
        """Verify current state against ``expected_state``.

        Supports hierarchical group membership verification: ``kind='group'``
        with ``transitive=True`` resolves the full member set.

        Parameters
        ----------
        descriptor:
            ``kind``, relevant refs.  For groups: ``group_ref``, ``member_ref``,
            optional ``transitive: bool`` (default True for this connector).
        expected_state:
            ``status`` (account), ``present`` (role/group/entitlement).
        """
        if self._force_timeout:
            return VerifyFactResult.timeout

        kind = descriptor.get('kind', '')
        account_ref = descriptor.get('account_ref', '')

        if kind == 'account':
            expected_status = expected_state.get('status', 'active')
            actual_status = self._state.account_status(account_ref)
            return VerifyFactResult.match if actual_status == expected_status else VerifyFactResult.mismatch

        if kind == 'role':
            role_ref = descriptor.get('role_ref', '')
            expected_present = expected_state.get('present', True)
            actual_present = self._state.has_role(account_ref, str(role_ref))
            return VerifyFactResult.match if actual_present == expected_present else VerifyFactResult.mismatch

        if kind == 'group':
            group_ref = descriptor.get('group_ref', '')
            member_ref = descriptor.get('member_ref', account_ref)
            transitive = descriptor.get('transitive', True)
            expected_present = expected_state.get('present', True)
            if transitive:
                actual_present = self._state.is_member(str(group_ref), str(member_ref))
            else:
                actual_present = str(member_ref) in self._state.group_members.get(str(group_ref), set())
            return VerifyFactResult.match if actual_present == expected_present else VerifyFactResult.mismatch

        if kind == 'entitlement':
            entitlement_ref = descriptor.get('entitlement_ref', '')
            expected_present = expected_state.get('present', True)
            actual_present = self._state.has_entitlement(account_ref, str(entitlement_ref))
            return VerifyFactResult.match if actual_present == expected_present else VerifyFactResult.mismatch

        # Unknown kind → conservative mismatch
        return VerifyFactResult.mismatch

    # ------------------------------------------------------------------
    # Generic dispatch
    # ------------------------------------------------------------------

    async def handle(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch ``operation`` to the matching handler method."""
        handlers: dict[str, Any] = {
            'account_create': self.account_create,
            'account_invite': self.account_invite,
            'account_activate': self.account_activate,
            'account_suspend': self.account_suspend,
            'account_disable': self.account_disable,
            'grant_role': self.grant_role,
            'revoke_role': self.revoke_role,
            'group_add': self.group_add,
            'group_remove': self.group_remove,
            'entitlement_attach': self.entitlement_attach,
            'entitlement_detach': self.entitlement_detach,
        }
        fn = handlers.get(operation)
        if fn is None:
            return {'status': 'error', 'error': {'message': f'Unknown operation: {operation!r}'}}
        return await fn(payload)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not value:
        raise ValueError(f'Missing required field {key!r} in payload')
    return str(value)

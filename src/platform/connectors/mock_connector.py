# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""In-process mock connector for Phase 19 testing (Step G1).

This is the **first reference connector** — a plain flat-structure mock that
supports all 11 operations required by the Exit Criterion and implements the
``verify_fact`` primitive.

Public surface
--------------
- ``MOCK_CONNECTOR_DESCRIPTOR`` — a ``ConnectorCapabilityDescriptor`` that can be
  registered via ``ConnectorRegistrationMessage`` in integration tests.
- ``VerifyFactResult`` — enum with three values: ``match | mismatch | timeout``.
- ``MockConnectorState`` — in-memory state (accounts, roles, groups, entitlements).
- ``MockConnectorHandler`` — processes operation payloads against the state and
  implements ``verify_fact``.
- ``MOCK_CONNECTOR_SUPPORTED_OPERATIONS`` — list of operation kind strings for
  assertion convenience.

Design contract
---------------
- Plain flat data model: each entity is keyed by ``(application, ref)``.
- ``verify_fact`` is synchronous / in-process; never times out by default.
  Set ``force_timeout=True`` on the handler to simulate timeout.
- Thread-safe: all state mutations go through dict operations under a lock.
- No I/O, no DB, no network.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from src.platform.connectors.registration_schemas import (
    AccountStatusTransitions,
    ConnectorCapabilityDescriptor,
    ConnectorOperationDescriptor,
    OperationDependencyRule,
)

# ---------------------------------------------------------------------------
# Supported operations (Exit Criterion list)
# ---------------------------------------------------------------------------

MOCK_CONNECTOR_SUPPORTED_OPERATIONS: list[str] = [
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

MOCK_CONNECTOR_DESCRIPTOR = ConnectorCapabilityDescriptor(
    operations=[
        # Account lifecycle — no prerequisites
        ConnectorOperationDescriptor(kind='account_create', dependency_rules=[]),
        ConnectorOperationDescriptor(kind='account_invite', dependency_rules=[]),
        ConnectorOperationDescriptor(kind='account_activate', dependency_rules=[]),
        ConnectorOperationDescriptor(kind='account_suspend', dependency_rules=[]),
        ConnectorOperationDescriptor(kind='account_disable', dependency_rules=[]),
        # Role operations — require an active account
        ConnectorOperationDescriptor(
            kind='grant_role',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active']),
            ],
        ),
        ConnectorOperationDescriptor(
            kind='revoke_role',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active']),
            ],
        ),
        # Group operations — require an active account
        ConnectorOperationDescriptor(
            kind='group_add',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active']),
            ],
        ),
        ConnectorOperationDescriptor(
            kind='group_remove',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active']),
            ],
        ),
        # Entitlement operations — require an active account
        ConnectorOperationDescriptor(
            kind='entitlement_attach',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active']),
            ],
        ),
        ConnectorOperationDescriptor(
            kind='entitlement_detach',
            dependency_rules=[
                OperationDependencyRule(resource='account', status=['active']),
            ],
        ),
    ],
    account_status=AccountStatusTransitions(
        transitions=[
            ('not_exists', 'invited'),
            ('invited', 'active'),
            ('active', 'suspended'),
            ('suspended', 'active'),
            ('active', 'disabled'),
        ]
    ),
    verify_fact_supported=True,
    supported_fact_kinds=['account', 'role', 'group', 'entitlement'],
)

# ---------------------------------------------------------------------------
# verify_fact result
# ---------------------------------------------------------------------------


class VerifyFactResult(str, Enum):
    """Result of a verify_fact call."""

    match = 'match'
    mismatch = 'mismatch'
    timeout = 'timeout'


# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------


@dataclass
class MockConnectorState:
    """In-memory state for the mock connector.

    All collections are keyed by ``ref`` (a string identifier).

    Account statuses use string values: ``invited``, ``active``, ``suspended``,
    ``disabled``. Absent key == ``not_exists``.

    Roles, groups and entitlements are stored as sets of refs per account.
    """

    # account_ref -> status
    accounts: dict[str, str] = field(default_factory=dict)

    # account_ref -> set of role_refs
    roles: dict[str, set[str]] = field(default_factory=dict)

    # account_ref -> set of group_refs
    groups: dict[str, set[str]] = field(default_factory=dict)

    # account_ref -> set of entitlement_refs
    entitlements: dict[str, set[str]] = field(default_factory=dict)

    def account_status(self, account_ref: str) -> str:
        """Return account status or ``not_exists`` when absent."""
        return self.accounts.get(account_ref, 'not_exists')

    def has_role(self, account_ref: str, role_ref: str) -> bool:
        return role_ref in self.roles.get(account_ref, set())

    def has_group(self, account_ref: str, group_ref: str) -> bool:
        return group_ref in self.groups.get(account_ref, set())

    def has_entitlement(self, account_ref: str, entitlement_ref: str) -> bool:
        return entitlement_ref in self.entitlements.get(account_ref, set())


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class MockConnectorHandler:
    """Processes operation payloads against a :class:`MockConnectorState`.

    Thread-safety: uses asyncio.Lock for all state mutations.

    Parameters
    ----------
    state:
        Shared in-memory state.  Defaults to a fresh empty state.
    force_timeout:
        When ``True``, every ``verify_fact`` call returns ``timeout``
        regardless of actual state.
    """

    def __init__(
        self,
        *,
        state: MockConnectorState | None = None,
        force_timeout: bool = False,
    ) -> None:
        self._state = state if state is not None else MockConnectorState()
        self._force_timeout = force_timeout
        self._lock = asyncio.Lock()

    @property
    def state(self) -> MockConnectorState:
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
        """Create account with status ``invited``."""
        account_ref = _require(payload, 'account_ref')
        async with self._lock:
            self._state.accounts[account_ref] = 'invited'
        return {'status': 'ok', 'account_ref': account_ref, 'account_status': 'invited'}

    async def account_activate(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Transition account to ``active``."""
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
        """Transition account to ``suspended``."""
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
        """Transition account to ``disabled``."""
        account_ref = _require(payload, 'account_ref')
        async with self._lock:
            current = self._state.account_status(account_ref)
            if current != 'active':
                return {
                    'status': 'error',
                    'error': {'message': f'Cannot disable from status {current!r}'},
                }
            self._state.accounts[account_ref] = 'disabled'
        return {'status': 'ok', 'account_ref': account_ref, 'account_status': 'disabled'}

    # ------------------------------------------------------------------
    # Role operations
    # ------------------------------------------------------------------

    async def grant_role(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Grant a role to an account."""
        account_ref = _require(payload, 'account_ref')
        role_ref = _require(payload, 'role_ref')
        async with self._lock:
            self._state.roles.setdefault(account_ref, set()).add(role_ref)
        return {'status': 'ok', 'account_ref': account_ref, 'role_ref': role_ref}

    async def revoke_role(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Revoke a role from an account."""
        account_ref = _require(payload, 'account_ref')
        role_ref = _require(payload, 'role_ref')
        async with self._lock:
            self._state.roles.get(account_ref, set()).discard(role_ref)
        return {'status': 'ok', 'account_ref': account_ref, 'role_ref': role_ref}

    # ------------------------------------------------------------------
    # Group operations
    # ------------------------------------------------------------------

    async def group_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Add an account to a group."""
        account_ref = _require(payload, 'account_ref')
        group_ref = _require(payload, 'group_ref')
        async with self._lock:
            self._state.groups.setdefault(account_ref, set()).add(group_ref)
        return {'status': 'ok', 'account_ref': account_ref, 'group_ref': group_ref}

    async def group_remove(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove an account from a group."""
        account_ref = _require(payload, 'account_ref')
        group_ref = _require(payload, 'group_ref')
        async with self._lock:
            self._state.groups.get(account_ref, set()).discard(group_ref)
        return {'status': 'ok', 'account_ref': account_ref, 'group_ref': group_ref}

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
        """Verify whether the current state matches ``expected_state``.

        Parameters
        ----------
        descriptor:
            Dict with ``kind`` and relevant refs (``account_ref``, ``role_ref``, etc.).
        expected_state:
            Dict with ``status`` or ``present`` key depending on fact kind.

        Returns
        -------
        VerifyFactResult.timeout
            When ``force_timeout=True`` on this handler.
        VerifyFactResult.match
            When the current state exactly matches ``expected_state``.
        VerifyFactResult.mismatch
            When the current state does not match.
        """
        if self._force_timeout:
            return VerifyFactResult.timeout

        kind = descriptor.get('kind', '')
        account_ref = descriptor.get('account_ref')

        if kind == 'account':
            expected_status = expected_state.get('status', 'active')
            actual_status = self._state.account_status(account_ref or '')
            return VerifyFactResult.match if actual_status == expected_status else VerifyFactResult.mismatch

        if kind == 'role':
            role_ref = descriptor.get('role_ref') or descriptor.get('target_descriptor', {}).get('role_ref', '')
            expected_present = expected_state.get('present', True)
            actual_present = self._state.has_role(account_ref or '', str(role_ref))
            return VerifyFactResult.match if actual_present == expected_present else VerifyFactResult.mismatch

        if kind == 'group':
            group_ref = descriptor.get('group_ref') or descriptor.get('target_descriptor', {}).get('group_ref', '')
            expected_present = expected_state.get('present', True)
            actual_present = self._state.has_group(account_ref or '', str(group_ref))
            return VerifyFactResult.match if actual_present == expected_present else VerifyFactResult.mismatch

        if kind == 'entitlement':
            entitlement_ref = descriptor.get('entitlement_ref') or descriptor.get('target_descriptor', {}).get(
                'entitlement_ref', ''
            )
            expected_present = expected_state.get('present', True)
            actual_present = self._state.has_entitlement(account_ref or '', str(entitlement_ref))
            return VerifyFactResult.match if actual_present == expected_present else VerifyFactResult.mismatch

        # Unknown fact kind → mismatch (conservative)
        return VerifyFactResult.mismatch

    # ------------------------------------------------------------------
    # Generic dispatch (used by RecordingStubRPCClient-compatible tests)
    # ------------------------------------------------------------------

    async def handle(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Dispatch ``operation`` to the matching method."""
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
        handler = handlers.get(operation)
        if handler is None:
            return {'status': 'error', 'error': {'message': f'Unknown operation: {operation!r}'}}
        return await handler(payload)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _require(payload: dict[str, Any], key: str) -> str:
    """Extract a required string field from a payload dict."""
    value = payload.get(key)
    if not value:
        raise ValueError(f'Missing required field {key!r} in payload')
    return str(value)

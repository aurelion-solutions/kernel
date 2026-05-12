# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the Phase 19 Step G1 mock connector.

Covers:
- Descriptor parse round-trip (MOCK_CONNECTOR_DESCRIPTOR)
- Descriptor contains all 11 required operations
- dependency_rules for role/group/entitlement operations require active account
- account_status.transitions matches spec
- verify_fact_supported = True, supported_fact_kinds correct
- verify_fact happy path: account, role, group, entitlement
- verify_fact mismatch path for each kind
- verify_fact timeout when force_timeout=True
- All 11 operations happy-path dispatch
- handle() unknown operation returns error envelope
- State isolation between handler instances
"""

from __future__ import annotations

import pytest
from src.platform.connectors.mock_connector import (
    MOCK_CONNECTOR_DESCRIPTOR,
    MOCK_CONNECTOR_SUPPORTED_OPERATIONS,
    MockConnectorHandler,
    MockConnectorState,
    VerifyFactResult,
)
from src.platform.connectors.registration_schemas import ConnectorCapabilityDescriptor

# ---------------------------------------------------------------------------
# Descriptor parse / round-trip
# ---------------------------------------------------------------------------


def test_descriptor_round_trip() -> None:
    """model_dump → model_validate must be lossless."""
    dumped = MOCK_CONNECTOR_DESCRIPTOR.model_dump()
    restored = ConnectorCapabilityDescriptor.model_validate(dumped)

    assert restored.verify_fact_supported is True
    assert restored.supported_fact_kinds == MOCK_CONNECTOR_DESCRIPTOR.supported_fact_kinds
    assert len(restored.operations) == len(MOCK_CONNECTOR_DESCRIPTOR.operations)
    assert restored.account_status.transitions == MOCK_CONNECTOR_DESCRIPTOR.account_status.transitions


def test_descriptor_operation_kinds_match_exit_criterion() -> None:
    """All 11 Exit Criterion operations must be present in the descriptor."""
    expected = set(MOCK_CONNECTOR_SUPPORTED_OPERATIONS)
    actual = {op.kind for op in MOCK_CONNECTOR_DESCRIPTOR.operations}
    assert actual == expected


def test_descriptor_has_11_operations() -> None:
    assert len(MOCK_CONNECTOR_DESCRIPTOR.operations) == 11


# ---------------------------------------------------------------------------
# dependency_rules per operation
# ---------------------------------------------------------------------------


def test_account_create_has_no_dependency_rules() -> None:
    op = _get_op('account_create')
    assert op.dependency_rules == []


def test_account_invite_has_no_dependency_rules() -> None:
    op = _get_op('account_invite')
    assert op.dependency_rules == []


def test_account_activate_has_no_dependency_rules() -> None:
    op = _get_op('account_activate')
    assert op.dependency_rules == []


def test_account_suspend_has_no_dependency_rules() -> None:
    op = _get_op('account_suspend')
    assert op.dependency_rules == []


def test_account_disable_has_no_dependency_rules() -> None:
    op = _get_op('account_disable')
    assert op.dependency_rules == []


def test_grant_role_requires_active_account() -> None:
    op = _get_op('grant_role')
    assert len(op.dependency_rules) == 1
    rule = op.dependency_rules[0]
    assert rule.resource == 'account'
    assert 'active' in rule.status


def test_revoke_role_requires_active_account() -> None:
    op = _get_op('revoke_role')
    assert len(op.dependency_rules) == 1
    rule = op.dependency_rules[0]
    assert rule.resource == 'account'
    assert 'active' in rule.status


def test_group_add_requires_active_account() -> None:
    op = _get_op('group_add')
    assert len(op.dependency_rules) == 1
    rule = op.dependency_rules[0]
    assert rule.resource == 'account'
    assert 'active' in rule.status


def test_group_remove_requires_active_account() -> None:
    op = _get_op('group_remove')
    assert len(op.dependency_rules) == 1
    rule = op.dependency_rules[0]
    assert rule.resource == 'account'
    assert 'active' in rule.status


def test_entitlement_attach_requires_active_account() -> None:
    op = _get_op('entitlement_attach')
    assert len(op.dependency_rules) == 1
    rule = op.dependency_rules[0]
    assert rule.resource == 'account'
    assert 'active' in rule.status


def test_entitlement_detach_requires_active_account() -> None:
    op = _get_op('entitlement_detach')
    assert len(op.dependency_rules) == 1
    rule = op.dependency_rules[0]
    assert rule.resource == 'account'
    assert 'active' in rule.status


# ---------------------------------------------------------------------------
# account_status transitions
# ---------------------------------------------------------------------------


def test_account_status_transitions_count() -> None:
    assert len(MOCK_CONNECTOR_DESCRIPTOR.account_status.transitions) == 5


def test_account_status_transitions_spec() -> None:
    expected = {
        ('not_exists', 'invited'),
        ('invited', 'active'),
        ('active', 'suspended'),
        ('suspended', 'active'),
        ('active', 'disabled'),
    }
    actual = set(map(tuple, MOCK_CONNECTOR_DESCRIPTOR.account_status.transitions))
    assert actual == expected


# ---------------------------------------------------------------------------
# verify_fact_supported + supported_fact_kinds
# ---------------------------------------------------------------------------


def test_verify_fact_supported_is_true() -> None:
    assert MOCK_CONNECTOR_DESCRIPTOR.verify_fact_supported is True


def test_supported_fact_kinds() -> None:
    assert set(MOCK_CONNECTOR_DESCRIPTOR.supported_fact_kinds) == {'account', 'role', 'group', 'entitlement'}


# ---------------------------------------------------------------------------
# verify_fact — happy path (match)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_fact_account_match() -> None:
    state = MockConnectorState(accounts={'alice': 'active'})
    handler = MockConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'account', 'account_ref': 'alice'},
        expected_state={'status': 'active'},
    )
    assert result == VerifyFactResult.match


@pytest.mark.asyncio
async def test_verify_fact_role_match() -> None:
    state = MockConnectorState(
        accounts={'alice': 'active'},
        roles={'alice': {'admin'}},
    )
    handler = MockConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'role', 'account_ref': 'alice', 'role_ref': 'admin'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.match


@pytest.mark.asyncio
async def test_verify_fact_group_match() -> None:
    state = MockConnectorState(
        accounts={'bob': 'active'},
        groups={'bob': {'engineering'}},
    )
    handler = MockConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'group', 'account_ref': 'bob', 'group_ref': 'engineering'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.match


@pytest.mark.asyncio
async def test_verify_fact_entitlement_match() -> None:
    state = MockConnectorState(
        accounts={'carol': 'active'},
        entitlements={'carol': {'read:reports'}},
    )
    handler = MockConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'entitlement', 'account_ref': 'carol', 'entitlement_ref': 'read:reports'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.match


# ---------------------------------------------------------------------------
# verify_fact — mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_fact_account_mismatch_wrong_status() -> None:
    state = MockConnectorState(accounts={'alice': 'suspended'})
    handler = MockConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'account', 'account_ref': 'alice'},
        expected_state={'status': 'active'},
    )
    assert result == VerifyFactResult.mismatch


@pytest.mark.asyncio
async def test_verify_fact_account_mismatch_not_exists() -> None:
    handler = MockConnectorHandler()

    result = await handler.verify_fact(
        descriptor={'kind': 'account', 'account_ref': 'ghost'},
        expected_state={'status': 'active'},
    )
    assert result == VerifyFactResult.mismatch


@pytest.mark.asyncio
async def test_verify_fact_role_mismatch_not_granted() -> None:
    state = MockConnectorState(accounts={'alice': 'active'})
    handler = MockConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'role', 'account_ref': 'alice', 'role_ref': 'admin'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.mismatch


@pytest.mark.asyncio
async def test_verify_fact_role_mismatch_present_but_expected_absent() -> None:
    state = MockConnectorState(
        accounts={'alice': 'active'},
        roles={'alice': {'admin'}},
    )
    handler = MockConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'role', 'account_ref': 'alice', 'role_ref': 'admin'},
        expected_state={'present': False},
    )
    assert result == VerifyFactResult.mismatch


@pytest.mark.asyncio
async def test_verify_fact_group_mismatch() -> None:
    state = MockConnectorState(accounts={'bob': 'active'})
    handler = MockConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'group', 'account_ref': 'bob', 'group_ref': 'engineering'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.mismatch


@pytest.mark.asyncio
async def test_verify_fact_entitlement_mismatch() -> None:
    state = MockConnectorState(accounts={'carol': 'active'})
    handler = MockConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'entitlement', 'account_ref': 'carol', 'entitlement_ref': 'read:reports'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.mismatch


# ---------------------------------------------------------------------------
# verify_fact — timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_fact_timeout_when_force_timeout() -> None:
    state = MockConnectorState(accounts={'alice': 'active'})
    handler = MockConnectorHandler(state=state, force_timeout=True)

    result = await handler.verify_fact(
        descriptor={'kind': 'account', 'account_ref': 'alice'},
        expected_state={'status': 'active'},
    )
    assert result == VerifyFactResult.timeout


# ---------------------------------------------------------------------------
# All 11 operations — happy-path dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_account_create() -> None:
    handler = MockConnectorHandler()
    result = await handler.handle('account_create', {'account_ref': 'alice'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('alice') == 'active'


@pytest.mark.asyncio
async def test_handle_account_invite() -> None:
    handler = MockConnectorHandler()
    result = await handler.handle('account_invite', {'account_ref': 'bob'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('bob') == 'invited'


@pytest.mark.asyncio
async def test_handle_account_activate() -> None:
    state = MockConnectorState(accounts={'carol': 'invited'})
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('account_activate', {'account_ref': 'carol'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('carol') == 'active'


@pytest.mark.asyncio
async def test_handle_account_activate_from_suspended() -> None:
    state = MockConnectorState(accounts={'dave': 'suspended'})
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('account_activate', {'account_ref': 'dave'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('dave') == 'active'


@pytest.mark.asyncio
async def test_handle_account_suspend() -> None:
    state = MockConnectorState(accounts={'eve': 'active'})
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('account_suspend', {'account_ref': 'eve'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('eve') == 'suspended'


@pytest.mark.asyncio
async def test_handle_account_disable() -> None:
    state = MockConnectorState(accounts={'frank': 'active'})
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('account_disable', {'account_ref': 'frank'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('frank') == 'disabled'


@pytest.mark.asyncio
async def test_handle_grant_role() -> None:
    state = MockConnectorState(accounts={'alice': 'active'})
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('grant_role', {'account_ref': 'alice', 'role_ref': 'admin'})
    assert result['status'] == 'ok'
    assert handler.state.has_role('alice', 'admin')


@pytest.mark.asyncio
async def test_handle_revoke_role() -> None:
    state = MockConnectorState(
        accounts={'alice': 'active'},
        roles={'alice': {'admin'}},
    )
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('revoke_role', {'account_ref': 'alice', 'role_ref': 'admin'})
    assert result['status'] == 'ok'
    assert not handler.state.has_role('alice', 'admin')


@pytest.mark.asyncio
async def test_handle_group_add() -> None:
    state = MockConnectorState(accounts={'alice': 'active'})
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('group_add', {'account_ref': 'alice', 'group_ref': 'devs'})
    assert result['status'] == 'ok'
    assert handler.state.has_group('alice', 'devs')


@pytest.mark.asyncio
async def test_handle_group_remove() -> None:
    state = MockConnectorState(
        accounts={'alice': 'active'},
        groups={'alice': {'devs'}},
    )
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('group_remove', {'account_ref': 'alice', 'group_ref': 'devs'})
    assert result['status'] == 'ok'
    assert not handler.state.has_group('alice', 'devs')


@pytest.mark.asyncio
async def test_handle_entitlement_attach() -> None:
    state = MockConnectorState(accounts={'alice': 'active'})
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('entitlement_attach', {'account_ref': 'alice', 'entitlement_ref': 'read:data'})
    assert result['status'] == 'ok'
    assert handler.state.has_entitlement('alice', 'read:data')


@pytest.mark.asyncio
async def test_handle_entitlement_detach() -> None:
    state = MockConnectorState(
        accounts={'alice': 'active'},
        entitlements={'alice': {'read:data'}},
    )
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('entitlement_detach', {'account_ref': 'alice', 'entitlement_ref': 'read:data'})
    assert result['status'] == 'ok'
    assert not handler.state.has_entitlement('alice', 'read:data')


# ---------------------------------------------------------------------------
# handle() — unknown operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_unknown_operation_returns_error() -> None:
    handler = MockConnectorHandler()
    result = await handler.handle('teleport_account', {'account_ref': 'alice'})
    assert result['status'] == 'error'
    assert 'teleport_account' in result['error']['message']


# ---------------------------------------------------------------------------
# State isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_isolation_between_handlers() -> None:
    """Two handler instances with separate states must not share data."""
    handler_a = MockConnectorHandler()
    handler_b = MockConnectorHandler()

    await handler_a.handle('account_create', {'account_ref': 'shared'})

    assert handler_a.state.account_status('shared') == 'active'
    assert handler_b.state.account_status('shared') == 'not_exists'


# ---------------------------------------------------------------------------
# State transitions — error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_activate_error_from_disabled() -> None:
    state = MockConnectorState(accounts={'alice': 'disabled'})
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('account_activate', {'account_ref': 'alice'})
    assert result['status'] == 'error'


@pytest.mark.asyncio
async def test_account_suspend_error_from_invited() -> None:
    state = MockConnectorState(accounts={'alice': 'invited'})
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('account_suspend', {'account_ref': 'alice'})
    assert result['status'] == 'error'


@pytest.mark.asyncio
async def test_account_disable_error_from_suspended() -> None:
    state = MockConnectorState(accounts={'alice': 'suspended'})
    handler = MockConnectorHandler(state=state)
    result = await handler.handle('account_disable', {'account_ref': 'alice'})
    assert result['status'] == 'error'


# ---------------------------------------------------------------------------
# verify_fact via target_descriptor dict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_fact_role_via_target_descriptor() -> None:
    """verify_fact should read role_ref from target_descriptor if not top-level."""
    state = MockConnectorState(
        accounts={'alice': 'active'},
        roles={'alice': {'admin'}},
    )
    handler = MockConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={
            'kind': 'role',
            'account_ref': 'alice',
            'target_descriptor': {'role_ref': 'admin'},
        },
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.match


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_op(kind: str):
    for op in MOCK_CONNECTOR_DESCRIPTOR.operations:
        if op.kind == kind:
            return op
    raise AssertionError(f'Operation {kind!r} not found in MOCK_CONNECTOR_DESCRIPTOR')

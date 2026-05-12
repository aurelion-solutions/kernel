# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the Phase 19 Step G2 hierarchical mock connector.

Covers:
- Descriptor parse round-trip (HIERARCHICAL_CONNECTOR_DESCRIPTOR)
- Descriptor contains all 11 required operations
- account_status transitions — richer graph than G1 (includes suspended->disabled)
- verify_fact_supported = True, supported_fact_kinds correct
- Hierarchical groups: group A contains group B contains user X → X transitively in A
- Hierarchical groups: cycle detection on group_add
- Transitive vs direct membership verify_fact
- Conditional grants: allowed with sufficient clearance
- Conditional grants: blocked without clearance
- Conditional grants: clearance from state.subject_attributes
- grant_role descriptor dependency_rules encodes conditional attribute check
- revoke_role has no clearance check
- Explicit invited → active transition flow
- verify_fact happy path for all 4 fact kinds
- verify_fact mismatch for all 4 fact kinds
- verify_fact timeout when force_timeout=True
- All 11 operations happy-path dispatch
- handle() unknown operation returns error envelope
- State isolation between handler instances
"""

from __future__ import annotations

import pytest
from src.platform.connectors.mock_connector import VerifyFactResult
from src.platform.connectors.mock_connector_hierarchical import (
    HIERARCHICAL_CONNECTOR_DESCRIPTOR,
    HIERARCHICAL_CONNECTOR_SUPPORTED_OPERATIONS,
    HierarchicalConnectorHandler,
    HierarchicalConnectorState,
)
from src.platform.connectors.registration_schemas import ConnectorCapabilityDescriptor

# ---------------------------------------------------------------------------
# Descriptor parse / round-trip
# ---------------------------------------------------------------------------


def test_descriptor_round_trip() -> None:
    """model_dump → model_validate must be lossless."""
    dumped = HIERARCHICAL_CONNECTOR_DESCRIPTOR.model_dump()
    restored = ConnectorCapabilityDescriptor.model_validate(dumped)

    assert restored.verify_fact_supported is True
    assert restored.supported_fact_kinds == HIERARCHICAL_CONNECTOR_DESCRIPTOR.supported_fact_kinds
    assert len(restored.operations) == len(HIERARCHICAL_CONNECTOR_DESCRIPTOR.operations)
    assert restored.account_status.transitions == HIERARCHICAL_CONNECTOR_DESCRIPTOR.account_status.transitions


def test_descriptor_operation_kinds_match_exit_criterion() -> None:
    """All 11 Exit Criterion operations must be present in the descriptor."""
    expected = set(HIERARCHICAL_CONNECTOR_SUPPORTED_OPERATIONS)
    actual = {op.kind for op in HIERARCHICAL_CONNECTOR_DESCRIPTOR.operations}
    assert actual == expected


def test_descriptor_has_11_operations() -> None:
    assert len(HIERARCHICAL_CONNECTOR_DESCRIPTOR.operations) == 11


# ---------------------------------------------------------------------------
# account_status transitions
# ---------------------------------------------------------------------------


def test_account_status_transitions_count() -> None:
    """This connector has 6 transitions (more than G1's 5)."""
    assert len(HIERARCHICAL_CONNECTOR_DESCRIPTOR.account_status.transitions) == 6


def test_account_status_transitions_include_suspended_to_disabled() -> None:
    """Richer graph: suspended → disabled is present here, not in G1."""
    transitions = set(map(tuple, HIERARCHICAL_CONNECTOR_DESCRIPTOR.account_status.transitions))
    assert ('suspended', 'disabled') in transitions


def test_account_status_transitions_include_invited_to_active() -> None:
    """Explicit invited → active must be in the graph."""
    transitions = set(map(tuple, HIERARCHICAL_CONNECTOR_DESCRIPTOR.account_status.transitions))
    assert ('invited', 'active') in transitions


def test_account_status_transitions_full_set() -> None:
    expected = {
        ('not_exists', 'invited'),
        ('invited', 'active'),
        ('active', 'suspended'),
        ('suspended', 'active'),
        ('active', 'disabled'),
        ('suspended', 'disabled'),
    }
    actual = set(map(tuple, HIERARCHICAL_CONNECTOR_DESCRIPTOR.account_status.transitions))
    assert actual == expected


# ---------------------------------------------------------------------------
# verify_fact_supported + supported_fact_kinds
# ---------------------------------------------------------------------------


def test_verify_fact_supported_is_true() -> None:
    assert HIERARCHICAL_CONNECTOR_DESCRIPTOR.verify_fact_supported is True


def test_supported_fact_kinds() -> None:
    assert set(HIERARCHICAL_CONNECTOR_DESCRIPTOR.supported_fact_kinds) == {
        'account',
        'role',
        'group',
        'entitlement',
    }


# ---------------------------------------------------------------------------
# Conditional grant dependency_rules
# ---------------------------------------------------------------------------


def test_grant_role_has_clearance_dependency_rule() -> None:
    """grant_role descriptor must encode the conditional clearance check."""
    op = _get_op('grant_role')
    resources = {r.resource for r in op.dependency_rules}
    assert 'subject_attribute' in resources


def test_grant_role_clearance_rule_statuses() -> None:
    op = _get_op('grant_role')
    rule = next(r for r in op.dependency_rules if r.resource == 'subject_attribute')
    assert 'clearance:secret' in rule.status
    assert 'clearance:top_secret' in rule.status


def test_revoke_role_has_no_clearance_rule() -> None:
    """revoke_role does not require clearance."""
    op = _get_op('revoke_role')
    resources = {r.resource for r in op.dependency_rules}
    assert 'subject_attribute' not in resources


# ---------------------------------------------------------------------------
# Conditional grants — handler enforcement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grant_role_allowed_with_secret_clearance() -> None:
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.grant_role(
        {
            'account_ref': 'alice',
            'role_ref': 'classified_reader',
            'subject_clearance': 'clearance:secret',
        }
    )
    assert result['status'] == 'ok'
    assert state.has_role('alice', 'classified_reader')


@pytest.mark.asyncio
async def test_grant_role_allowed_with_top_secret_clearance() -> None:
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.grant_role(
        {
            'account_ref': 'alice',
            'role_ref': 'ts_reader',
            'subject_clearance': 'clearance:top_secret',
        }
    )
    assert result['status'] == 'ok'
    assert state.has_role('alice', 'ts_reader')


@pytest.mark.asyncio
async def test_grant_role_blocked_without_clearance() -> None:
    state = HierarchicalConnectorState(accounts={'bob': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.grant_role(
        {
            'account_ref': 'bob',
            'role_ref': 'classified_reader',
            'subject_clearance': 'clearance:public',
        }
    )
    assert result['status'] == 'error'
    assert 'clearance' in result['error']['message']
    assert not state.has_role('bob', 'classified_reader')


@pytest.mark.asyncio
async def test_grant_role_blocked_with_no_clearance_key() -> None:
    """Omitting subject_clearance falls back to state attributes (none present)."""
    state = HierarchicalConnectorState(accounts={'charlie': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.grant_role({'account_ref': 'charlie', 'role_ref': 'reader'})
    assert result['status'] == 'error'


@pytest.mark.asyncio
async def test_grant_role_allowed_via_state_attributes() -> None:
    """Clearance resolved from state.subject_attributes when not in payload."""
    state = HierarchicalConnectorState(
        accounts={'dana': 'active'},
        subject_attributes={'dana': {'clearance:secret', 'dept:research'}},
    )
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.grant_role({'account_ref': 'dana', 'role_ref': 'lab_reader'})
    assert result['status'] == 'ok'
    assert state.has_role('dana', 'lab_reader')


# ---------------------------------------------------------------------------
# Hierarchical groups
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_add_account_as_leaf_member() -> None:
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.group_add({'group_ref': 'eng', 'member_ref': 'alice'})
    assert result['status'] == 'ok'
    assert 'alice' in state.group_members.get('eng', set())


@pytest.mark.asyncio
async def test_group_add_nested_group() -> None:
    """group B can be added as a member of group A."""
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    # Build: alice ∈ frontend; frontend ∈ eng
    await handler.group_add({'group_ref': 'frontend', 'member_ref': 'alice'})
    result = await handler.group_add({'group_ref': 'eng', 'member_ref': 'frontend'})
    assert result['status'] == 'ok'
    assert 'frontend' in state.group_members.get('eng', set())


@pytest.mark.asyncio
async def test_transitive_membership_two_levels() -> None:
    """alice ∈ frontend ∈ eng → alice is transitive member of eng."""
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    await handler.group_add({'group_ref': 'frontend', 'member_ref': 'alice'})
    await handler.group_add({'group_ref': 'eng', 'member_ref': 'frontend'})

    assert state.is_member('eng', 'alice') is True
    assert state.is_member('frontend', 'alice') is True


@pytest.mark.asyncio
async def test_transitive_membership_three_levels() -> None:
    """group A → group B → group C → user X: X is transitive member of A."""
    state = HierarchicalConnectorState(accounts={'userX': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    await handler.group_add({'group_ref': 'C', 'member_ref': 'userX'})
    await handler.group_add({'group_ref': 'B', 'member_ref': 'C'})
    await handler.group_add({'group_ref': 'A', 'member_ref': 'B'})

    assert state.is_member('A', 'userX') is True
    assert state.is_member('B', 'userX') is True
    assert state.is_member('C', 'userX') is True


@pytest.mark.asyncio
async def test_non_member_not_reported_transitively() -> None:
    state = HierarchicalConnectorState(accounts={'alice': 'active', 'bob': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    await handler.group_add({'group_ref': 'eng', 'member_ref': 'alice'})
    assert state.is_member('eng', 'bob') is False


@pytest.mark.asyncio
async def test_group_remove_removes_direct_member() -> None:
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    await handler.group_add({'group_ref': 'eng', 'member_ref': 'alice'})
    await handler.group_remove({'group_ref': 'eng', 'member_ref': 'alice'})
    assert 'alice' not in state.group_members.get('eng', set())


@pytest.mark.asyncio
async def test_cycle_detection_prevents_self_loop() -> None:
    """Adding group X to itself must be rejected."""
    state = HierarchicalConnectorState()
    handler = HierarchicalConnectorHandler(state=state)
    # Seed group_members so cycle detection applies
    state.group_members['X'] = set()
    result = await handler.group_add({'group_ref': 'X', 'member_ref': 'X'})
    assert result['status'] == 'error'
    assert 'cycle' in result['error']['message']


@pytest.mark.asyncio
async def test_cycle_detection_prevents_indirect_cycle() -> None:
    """A → B → A cycle must be detected and rejected."""
    state = HierarchicalConnectorState()
    handler = HierarchicalConnectorHandler(state=state)
    state.group_members['A'] = {'B'}
    state.group_members['B'] = set()
    result = await handler.group_add({'group_ref': 'B', 'member_ref': 'A'})
    assert result['status'] == 'error'
    assert 'cycle' in result['error']['message']


# ---------------------------------------------------------------------------
# Explicit invited → active transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invite_then_activate_flow() -> None:
    """Full flow: account_invite → account_activate succeeds."""
    handler = HierarchicalConnectorHandler()
    await handler.account_invite({'account_ref': 'newbie'})
    assert handler.state.account_status('newbie') == 'invited'

    result = await handler.account_activate({'account_ref': 'newbie'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('newbie') == 'active'


@pytest.mark.asyncio
async def test_activate_from_not_exists_fails() -> None:
    handler = HierarchicalConnectorHandler()
    result = await handler.account_activate({'account_ref': 'ghost'})
    assert result['status'] == 'error'


@pytest.mark.asyncio
async def test_suspend_then_disable_flow() -> None:
    """suspended → disabled is a valid transition in this connector."""
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    await handler.account_suspend({'account_ref': 'alice'})
    result = await handler.account_disable({'account_ref': 'alice'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('alice') == 'disabled'


@pytest.mark.asyncio
async def test_disable_from_disabled_fails() -> None:
    state = HierarchicalConnectorState(accounts={'alice': 'disabled'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.account_disable({'account_ref': 'alice'})
    assert result['status'] == 'error'


# ---------------------------------------------------------------------------
# verify_fact — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_fact_account_match() -> None:
    state = HierarchicalConnectorState(accounts={'alice': 'invited'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.verify_fact(
        descriptor={'kind': 'account', 'account_ref': 'alice'},
        expected_state={'status': 'invited'},
    )
    assert result == VerifyFactResult.match


@pytest.mark.asyncio
async def test_verify_fact_role_match() -> None:
    state = HierarchicalConnectorState(
        accounts={'alice': 'active'},
        roles={'alice': {'reader'}},
    )
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.verify_fact(
        descriptor={'kind': 'role', 'account_ref': 'alice', 'role_ref': 'reader'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.match


@pytest.mark.asyncio
async def test_verify_fact_group_transitive_match() -> None:
    """verify_fact with kind=group and transitive=True resolves hierarchy."""
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    state.group_members['frontend'] = {'alice'}
    state.group_members['eng'] = {'frontend'}
    handler = HierarchicalConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'group', 'group_ref': 'eng', 'member_ref': 'alice', 'transitive': True},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.match


@pytest.mark.asyncio
async def test_verify_fact_group_direct_mismatch_when_only_transitive() -> None:
    """If transitive=False, alice (only transitively in eng) → mismatch."""
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    state.group_members['frontend'] = {'alice'}
    state.group_members['eng'] = {'frontend'}
    handler = HierarchicalConnectorHandler(state=state)

    result = await handler.verify_fact(
        descriptor={'kind': 'group', 'group_ref': 'eng', 'member_ref': 'alice', 'transitive': False},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.mismatch


@pytest.mark.asyncio
async def test_verify_fact_entitlement_match() -> None:
    state = HierarchicalConnectorState(
        accounts={'alice': 'active'},
        entitlements={'alice': {'read:reports'}},
    )
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.verify_fact(
        descriptor={'kind': 'entitlement', 'account_ref': 'alice', 'entitlement_ref': 'read:reports'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.match


# ---------------------------------------------------------------------------
# verify_fact — mismatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_fact_account_mismatch() -> None:
    state = HierarchicalConnectorState(accounts={'alice': 'suspended'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.verify_fact(
        descriptor={'kind': 'account', 'account_ref': 'alice'},
        expected_state={'status': 'active'},
    )
    assert result == VerifyFactResult.mismatch


@pytest.mark.asyncio
async def test_verify_fact_role_mismatch_not_granted() -> None:
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.verify_fact(
        descriptor={'kind': 'role', 'account_ref': 'alice', 'role_ref': 'admin'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.mismatch


@pytest.mark.asyncio
async def test_verify_fact_role_mismatch_expected_absent_but_present() -> None:
    state = HierarchicalConnectorState(
        accounts={'alice': 'active'},
        roles={'alice': {'admin'}},
    )
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.verify_fact(
        descriptor={'kind': 'role', 'account_ref': 'alice', 'role_ref': 'admin'},
        expected_state={'present': False},
    )
    assert result == VerifyFactResult.mismatch


@pytest.mark.asyncio
async def test_verify_fact_group_mismatch_not_member() -> None:
    state = HierarchicalConnectorState(accounts={'bob': 'active'})
    state.group_members['eng'] = set()
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.verify_fact(
        descriptor={'kind': 'group', 'group_ref': 'eng', 'member_ref': 'bob'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.mismatch


@pytest.mark.asyncio
async def test_verify_fact_entitlement_mismatch() -> None:
    state = HierarchicalConnectorState(accounts={'carol': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.verify_fact(
        descriptor={'kind': 'entitlement', 'account_ref': 'carol', 'entitlement_ref': 'read:reports'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.mismatch


# ---------------------------------------------------------------------------
# verify_fact — timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_fact_timeout_when_force_timeout_account() -> None:
    state = HierarchicalConnectorState(accounts={'alice': 'active'})
    handler = HierarchicalConnectorHandler(state=state, force_timeout=True)
    result = await handler.verify_fact(
        descriptor={'kind': 'account', 'account_ref': 'alice'},
        expected_state={'status': 'active'},
    )
    assert result == VerifyFactResult.timeout


@pytest.mark.asyncio
async def test_verify_fact_timeout_when_force_timeout_role() -> None:
    state = HierarchicalConnectorState(
        accounts={'alice': 'active'},
        roles={'alice': {'admin'}},
    )
    handler = HierarchicalConnectorHandler(state=state, force_timeout=True)
    result = await handler.verify_fact(
        descriptor={'kind': 'role', 'account_ref': 'alice', 'role_ref': 'admin'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.timeout


@pytest.mark.asyncio
async def test_verify_fact_timeout_when_force_timeout_group() -> None:
    state = HierarchicalConnectorState()
    state.group_members['eng'] = {'alice'}
    handler = HierarchicalConnectorHandler(state=state, force_timeout=True)
    result = await handler.verify_fact(
        descriptor={'kind': 'group', 'group_ref': 'eng', 'member_ref': 'alice'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.timeout


@pytest.mark.asyncio
async def test_verify_fact_timeout_when_force_timeout_entitlement() -> None:
    state = HierarchicalConnectorState(
        accounts={'alice': 'active'},
        entitlements={'alice': {'read:data'}},
    )
    handler = HierarchicalConnectorHandler(state=state, force_timeout=True)
    result = await handler.verify_fact(
        descriptor={'kind': 'entitlement', 'account_ref': 'alice', 'entitlement_ref': 'read:data'},
        expected_state={'present': True},
    )
    assert result == VerifyFactResult.timeout


# ---------------------------------------------------------------------------
# All 11 operations — happy-path dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_account_create() -> None:
    handler = HierarchicalConnectorHandler()
    result = await handler.handle('account_create', {'account_ref': 'alice'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('alice') == 'active'


@pytest.mark.asyncio
async def test_handle_account_invite() -> None:
    handler = HierarchicalConnectorHandler()
    result = await handler.handle('account_invite', {'account_ref': 'bob'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('bob') == 'invited'


@pytest.mark.asyncio
async def test_handle_account_activate() -> None:
    state = HierarchicalConnectorState(accounts={'carol': 'invited'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.handle('account_activate', {'account_ref': 'carol'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('carol') == 'active'


@pytest.mark.asyncio
async def test_handle_account_suspend() -> None:
    state = HierarchicalConnectorState(accounts={'dave': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.handle('account_suspend', {'account_ref': 'dave'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('dave') == 'suspended'


@pytest.mark.asyncio
async def test_handle_account_disable_from_active() -> None:
    state = HierarchicalConnectorState(accounts={'eve': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.handle('account_disable', {'account_ref': 'eve'})
    assert result['status'] == 'ok'
    assert handler.state.account_status('eve') == 'disabled'


@pytest.mark.asyncio
async def test_handle_grant_role() -> None:
    state = HierarchicalConnectorState(accounts={'frank': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.handle(
        'grant_role',
        {'account_ref': 'frank', 'role_ref': 'reader', 'subject_clearance': 'clearance:secret'},
    )
    assert result['status'] == 'ok'
    assert handler.state.has_role('frank', 'reader')


@pytest.mark.asyncio
async def test_handle_revoke_role() -> None:
    state = HierarchicalConnectorState(
        accounts={'grace': 'active'},
        roles={'grace': {'reader'}},
    )
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.handle('revoke_role', {'account_ref': 'grace', 'role_ref': 'reader'})
    assert result['status'] == 'ok'
    assert not handler.state.has_role('grace', 'reader')


@pytest.mark.asyncio
async def test_handle_group_add() -> None:
    state = HierarchicalConnectorState(accounts={'heidi': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.handle('group_add', {'group_ref': 'team', 'member_ref': 'heidi'})
    assert result['status'] == 'ok'
    assert 'heidi' in handler.state.group_members.get('team', set())


@pytest.mark.asyncio
async def test_handle_group_remove() -> None:
    state = HierarchicalConnectorState(accounts={'ivan': 'active'})
    state.group_members['team'] = {'ivan'}
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.handle('group_remove', {'group_ref': 'team', 'member_ref': 'ivan'})
    assert result['status'] == 'ok'
    assert 'ivan' not in handler.state.group_members.get('team', set())


@pytest.mark.asyncio
async def test_handle_entitlement_attach() -> None:
    state = HierarchicalConnectorState(accounts={'judy': 'active'})
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.handle('entitlement_attach', {'account_ref': 'judy', 'entitlement_ref': 'read:data'})
    assert result['status'] == 'ok'
    assert handler.state.has_entitlement('judy', 'read:data')


@pytest.mark.asyncio
async def test_handle_entitlement_detach() -> None:
    state = HierarchicalConnectorState(
        accounts={'kim': 'active'},
        entitlements={'kim': {'read:data'}},
    )
    handler = HierarchicalConnectorHandler(state=state)
    result = await handler.handle('entitlement_detach', {'account_ref': 'kim', 'entitlement_ref': 'read:data'})
    assert result['status'] == 'ok'
    assert not handler.state.has_entitlement('kim', 'read:data')


# ---------------------------------------------------------------------------
# handle() — unknown operation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_unknown_operation_returns_error() -> None:
    handler = HierarchicalConnectorHandler()
    result = await handler.handle('quantum_tunnel', {'account_ref': 'alice'})
    assert result['status'] == 'error'
    assert 'quantum_tunnel' in result['error']['message']


# ---------------------------------------------------------------------------
# State isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_state_isolation_between_handlers() -> None:
    handler_a = HierarchicalConnectorHandler()
    handler_b = HierarchicalConnectorHandler()

    await handler_a.handle('account_create', {'account_ref': 'shared'})

    assert handler_a.state.account_status('shared') == 'active'
    assert handler_b.state.account_status('shared') == 'not_exists'


# ---------------------------------------------------------------------------
# resolve_members edge cases
# ---------------------------------------------------------------------------


def test_resolve_members_empty_group() -> None:
    state = HierarchicalConnectorState()
    state.group_members['empty'] = set()
    assert state.resolve_members('empty') == set()


def test_resolve_members_nonexistent_group() -> None:
    state = HierarchicalConnectorState()
    assert state.resolve_members('ghost') == set()


def test_resolve_members_multiple_leaf_accounts() -> None:
    state = HierarchicalConnectorState(accounts={'a': 'active', 'b': 'active', 'c': 'active'})
    state.group_members['team'] = {'a', 'b', 'c'}
    assert state.resolve_members('team') == {'a', 'b', 'c'}


def test_resolve_members_mixed_accounts_and_groups() -> None:
    """group = {alice, sub_group}; sub_group = {bob} → resolve returns {alice, bob}."""
    state = HierarchicalConnectorState(accounts={'alice': 'active', 'bob': 'active'})
    state.group_members['sub_group'] = {'bob'}
    state.group_members['team'] = {'alice', 'sub_group'}
    members = state.resolve_members('team')
    assert members == {'alice', 'bob'}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_op(kind: str):
    for op in HIERARCHICAL_CONNECTOR_DESCRIPTOR.operations:
        if op.kind == kind:
            return op
    raise AssertionError(f'Operation {kind!r} not found in HIERARCHICAL_CONNECTOR_DESCRIPTOR')

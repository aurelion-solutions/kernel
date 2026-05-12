# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for connector descriptor extension (Phase 19 Step B1).

Covers:
- Parse / round-trip of ConnectorCapabilityDescriptor
- Registration consumer persists descriptor
- Repository get_connector_descriptor returns stored JSONB
- ConnectorInstanceService.get_descriptor returns parsed model
- Backward-compat: messages without descriptor field accepted
"""

import pytest
from src.platform.connectors.registration_consumer import handle_connector_registration
from src.platform.connectors.registration_schemas import (
    AccountStatusTransitions,
    ConnectorCapabilityDescriptor,
    ConnectorOperationDescriptor,
    ConnectorRegistrationMessage,
    OperationDependencyRule,
)
from src.platform.connectors.repository import get_connector_descriptor
from src.platform.connectors.service import ConnectorInstanceService

# ---------------------------------------------------------------------------
# Schema parse / round-trip (unit, no DB)
# ---------------------------------------------------------------------------


def test_operation_dependency_rule_parse() -> None:
    rule = OperationDependencyRule(resource='account', status=['active'])
    assert rule.resource == 'account'
    assert rule.status == ['active']


def test_connector_operation_descriptor_defaults() -> None:
    op = ConnectorOperationDescriptor(kind='account_create')
    assert op.kind == 'account_create'
    assert op.dependency_rules == []


def test_connector_operation_descriptor_with_deps() -> None:
    op = ConnectorOperationDescriptor(
        kind='role_grant',
        dependency_rules=[OperationDependencyRule(resource='account', status=['active'])],
    )
    assert len(op.dependency_rules) == 1
    assert op.dependency_rules[0].resource == 'account'


def test_account_status_transitions_defaults() -> None:
    ast = AccountStatusTransitions()
    assert ast.transitions == []


def test_account_status_transitions_parse() -> None:
    ast = AccountStatusTransitions(
        transitions=[
            ('not_exists', 'invited'),
            ('invited', 'active'),
            ('active', 'suspended'),
            ('suspended', 'active'),
            ('active', 'disabled'),
        ]
    )
    assert len(ast.transitions) == 5
    assert ast.transitions[0] == ('not_exists', 'invited')


def test_capability_descriptor_defaults() -> None:
    desc = ConnectorCapabilityDescriptor()
    assert desc.operations == []
    assert desc.verify_fact_supported is False
    assert desc.supported_fact_kinds == []
    assert desc.account_status.transitions == []


def test_capability_descriptor_full() -> None:
    desc = ConnectorCapabilityDescriptor(
        operations=[
            ConnectorOperationDescriptor(
                kind='account_create',
                dependency_rules=[],
            ),
            ConnectorOperationDescriptor(
                kind='role_grant',
                dependency_rules=[OperationDependencyRule(resource='account', status=['active'])],
            ),
        ],
        account_status=AccountStatusTransitions(
            transitions=[
                ('not_exists', 'invited'),
                ('invited', 'active'),
            ]
        ),
        verify_fact_supported=True,
        supported_fact_kinds=['role_grant', 'group_membership'],
    )
    assert len(desc.operations) == 2
    assert desc.verify_fact_supported is True
    assert desc.supported_fact_kinds == ['role_grant', 'group_membership']


def test_capability_descriptor_round_trip() -> None:
    """model_dump → model_validate must be lossless."""
    original = ConnectorCapabilityDescriptor(
        operations=[
            ConnectorOperationDescriptor(
                kind='role_grant',
                dependency_rules=[OperationDependencyRule(resource='account', status=['active'])],
            )
        ],
        account_status=AccountStatusTransitions(transitions=[('not_exists', 'active'), ('active', 'disabled')]),
        verify_fact_supported=True,
        supported_fact_kinds=['role_grant'],
    )
    dumped = original.model_dump()
    restored = ConnectorCapabilityDescriptor.model_validate(dumped)

    assert restored.operations[0].kind == 'role_grant'
    assert restored.operations[0].dependency_rules[0].resource == 'account'
    assert restored.account_status.transitions == [('not_exists', 'active'), ('active', 'disabled')]
    assert restored.verify_fact_supported is True
    assert restored.supported_fact_kinds == ['role_grant']


def test_registration_message_without_descriptor() -> None:
    """Messages without descriptor field must still parse (backward compat)."""
    msg = ConnectorRegistrationMessage(
        event_type='connector.registered',
        instance_id='legacy-connector',
        tags=['jira'],
    )
    assert msg.descriptor is None


def test_registration_message_with_descriptor() -> None:
    msg = ConnectorRegistrationMessage(
        event_type='connector.registered',
        instance_id='rich-connector',
        tags=['github'],
        descriptor=ConnectorCapabilityDescriptor(
            verify_fact_supported=True,
            supported_fact_kinds=['role_grant'],
        ),
    )
    assert msg.descriptor is not None
    assert msg.descriptor.verify_fact_supported is True


# ---------------------------------------------------------------------------
# Integration tests (require DB via session_factory fixture)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_consumer_persists_descriptor(session_factory) -> None:
    """Full pipeline: consumer receives message with descriptor → stored in DB."""
    message = {
        'event_type': 'connector.registered',
        'instance_id': 'rich-conn-1',
        'tags': ['github'],
        'descriptor': {
            'operations': [
                {
                    'kind': 'account_create',
                    'dependency_rules': [],
                },
                {
                    'kind': 'role_grant',
                    'dependency_rules': [{'resource': 'account', 'status': ['active']}],
                },
            ],
            'account_status': {
                'transitions': [
                    ['not_exists', 'invited'],
                    ['invited', 'active'],
                    ['active', 'suspended'],
                    ['suspended', 'active'],
                    ['active', 'disabled'],
                ]
            },
            'verify_fact_supported': True,
            'supported_fact_kinds': ['role_grant', 'group_membership'],
        },
    }

    await handle_connector_registration(session_factory, message)

    async with session_factory() as session:
        raw = await get_connector_descriptor(session, 'rich-conn-1')

    assert raw is not None
    assert raw['verify_fact_supported'] is True
    assert len(raw['supported_fact_kinds']) == 2
    assert len(raw['operations']) == 2


@pytest.mark.asyncio
async def test_registration_consumer_no_descriptor_preserves_null(session_factory) -> None:
    """Message without descriptor leaves the column as NULL (first registration)."""
    message = {
        'event_type': 'connector.registered',
        'instance_id': 'plain-conn-1',
        'tags': ['ldap'],
    }

    await handle_connector_registration(session_factory, message)

    async with session_factory() as session:
        raw = await get_connector_descriptor(session, 'plain-conn-1')

    assert raw is None


@pytest.mark.asyncio
async def test_registration_consumer_heartbeat_preserves_descriptor(session_factory) -> None:
    """Heartbeat message without descriptor should NOT overwrite existing descriptor."""
    # First: register with a full descriptor
    register_msg = {
        'event_type': 'connector.registered',
        'instance_id': 'heartbeat-conn-1',
        'tags': ['github'],
        'descriptor': {
            'operations': [],
            'account_status': {'transitions': [['not_exists', 'active']]},
            'verify_fact_supported': True,
            'supported_fact_kinds': ['role_grant'],
        },
    }
    await handle_connector_registration(session_factory, register_msg)

    # Second: send heartbeat without descriptor
    heartbeat_msg = {
        'event_type': 'connector.heartbeat',
        'instance_id': 'heartbeat-conn-1',
        'tags': ['github'],
    }
    await handle_connector_registration(session_factory, heartbeat_msg)

    async with session_factory() as session:
        raw = await get_connector_descriptor(session, 'heartbeat-conn-1')

    # Descriptor should still be there — heartbeat did not overwrite
    assert raw is not None
    assert raw['verify_fact_supported'] is True


@pytest.mark.asyncio
async def test_service_get_descriptor_returns_parsed_model(session_factory) -> None:
    """ConnectorInstanceService.get_descriptor returns parsed ConnectorCapabilityDescriptor."""
    message = {
        'event_type': 'connector.registered',
        'instance_id': 'service-desc-conn-1',
        'tags': ['okta'],
        'descriptor': {
            'operations': [
                {
                    'kind': 'account_invite',
                    'dependency_rules': [],
                }
            ],
            'account_status': {
                'transitions': [
                    ['not_exists', 'invited'],
                    ['invited', 'active'],
                ]
            },
            'verify_fact_supported': False,
            'supported_fact_kinds': [],
        },
    }

    await handle_connector_registration(session_factory, message)

    service = ConnectorInstanceService()
    async with session_factory() as session:
        descriptor = await service.get_descriptor(session, 'service-desc-conn-1')

    assert descriptor is not None
    assert isinstance(descriptor, ConnectorCapabilityDescriptor)
    assert descriptor.operations[0].kind == 'account_invite'
    assert descriptor.account_status.transitions == [('not_exists', 'invited'), ('invited', 'active')]
    assert descriptor.verify_fact_supported is False


@pytest.mark.asyncio
async def test_service_get_descriptor_returns_none_for_missing(session_factory) -> None:
    service = ConnectorInstanceService()
    async with session_factory() as session:
        descriptor = await service.get_descriptor(session, 'nonexistent-connector')

    assert descriptor is None


@pytest.mark.asyncio
async def test_registration_updates_descriptor_on_re_register(session_factory) -> None:
    """Re-registration with a new descriptor updates the stored value."""
    first_msg = {
        'event_type': 'connector.registered',
        'instance_id': 'update-desc-conn-1',
        'tags': ['github'],
        'descriptor': {
            'operations': [],
            'account_status': {'transitions': []},
            'verify_fact_supported': False,
            'supported_fact_kinds': [],
        },
    }
    await handle_connector_registration(session_factory, first_msg)

    second_msg = {
        'event_type': 'connector.registered',
        'instance_id': 'update-desc-conn-1',
        'tags': ['github'],
        'descriptor': {
            'operations': [{'kind': 'account_create', 'dependency_rules': []}],
            'account_status': {'transitions': [['not_exists', 'active']]},
            'verify_fact_supported': True,
            'supported_fact_kinds': ['role_grant'],
        },
    }
    await handle_connector_registration(session_factory, second_msg)

    service = ConnectorInstanceService()
    async with session_factory() as session:
        descriptor = await service.get_descriptor(session, 'update-desc-conn-1')

    assert descriptor is not None
    assert descriptor.verify_fact_supported is True
    assert len(descriptor.operations) == 1
    assert descriptor.operations[0].kind == 'account_create'

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for ``platform.connectors.client.ConnectorClient`` and ``result_expansion``."""

from collections.abc import Iterable
from typing import Any
import uuid

import pytest
from src.platform.applications.models import Application
from src.platform.connectors.result_expansion import expand_records_from_response
from src.platform.connectors.tests.support import (
    RecordingStubRPCClient,
    connector_client_with_stub,
)

MOCK_INSTANCE_ID = 'mock-connector'


class StubStorage:
    def __init__(self, records: Iterable[dict[str, Any]]) -> None:
        self.records = list(records)
        self.keys_read: list[str] = []

    def read_batch(self, storage_key: str):
        self.keys_read.append(storage_key)
        for item in self.records:
            yield item


class StubLakeFactory:
    def __init__(self, storage: StubStorage) -> None:
        self.storage = storage
        self.providers_requested: list[str] = []

    def get(self, provider: str) -> StubStorage:
        self.providers_requested.append(provider)
        return self.storage


def make_app() -> Application:
    _suffix = uuid.uuid4().hex[:12]
    return Application(
        name=f'test-app-connector-{_suffix}',
        code=f'test-app-c-{_suffix}',
        config={'lake_provider': 'file'},
        required_connector_tags=[],
    )


def test_expand_records_inline_payload():
    raw = {
        'status': 'ok',
        'payload': {
            'accounts': [{'identifier': 'u1', 'username': 'alice'}],
        },
    }
    lake = StubLakeFactory(StubStorage([]))
    result = expand_records_from_response(raw, list_key='accounts', lake_factory=lake)
    assert result == {'accounts': [{'identifier': 'u1', 'username': 'alice'}]}


def test_expand_records_from_result_storage_ref():
    raw = {
        'status': 'ok',
        'result_storage_ref': {
            'provider': 'file',
            'storage_key': 'accounts/test.jsonl',
        },
    }
    storage = StubStorage([{'identifier': 'u1', 'username': 'alice'}])
    lake_factory = StubLakeFactory(storage)
    result = expand_records_from_response(raw, list_key='accounts', lake_factory=lake_factory)
    assert result == {'accounts': [{'identifier': 'u1', 'username': 'alice'}]}
    assert lake_factory.providers_requested == ['file']
    assert storage.keys_read == ['accounts/test.jsonl']


@pytest.mark.asyncio
async def test_invoke_returns_raw_envelope_for_list_operation():
    stub = RecordingStubRPCClient(
        {
            'list_accounts': {
                'status': 'ok',
                'payload': {
                    'accounts': [{'identifier': 'u1', 'username': 'alice'}],
                },
            }
        }
    )
    connector = connector_client_with_stub(stub)
    app = make_app()
    payload = {'config': app.config}

    raw = await connector.invoke(
        MOCK_INSTANCE_ID,
        'list_accounts',
        payload,
        result_storage_requested=True,
    )

    assert raw['payload']['accounts'][0]['username'] == 'alice'
    call0 = stub.calls[0]
    assert call0['instance_id'] == MOCK_INSTANCE_ID
    assert call0['operation'] == 'list_accounts'
    assert call0['payload']['config'] == {'lake_provider': 'file'}
    assert call0['result_storage_requested'] is True


@pytest.mark.asyncio
async def test_invoke_create_forwards_command_raw_envelope():
    stub = RecordingStubRPCClient(
        {
            'create_account': {
                'status': 'ok',
                'payload': {'username': 'alice', 'email': 'alice@example.com'},
            }
        }
    )
    connector = connector_client_with_stub(stub)
    app = make_app()

    result = await connector.invoke(
        MOCK_INSTANCE_ID,
        'create_account',
        {
            'config': app.config,
            'username': 'alice',
            'email': 'alice@example.com',
        },
        result_storage_requested=False,
    )

    assert result['payload']['username'] == 'alice'
    call0 = stub.calls[0]
    assert call0['instance_id'] == MOCK_INSTANCE_ID
    assert call0['operation'] == 'create_account'
    assert call0['payload']['username'] == 'alice'
    assert call0['payload']['email'] == 'alice@example.com'
    assert call0['payload']['config'] == {'lake_provider': 'file'}
    assert call0['result_storage_requested'] is False


def test_expand_records_raises_when_no_payload_or_storage_ref():
    raw = {'status': 'ok'}
    lake = StubLakeFactory(StubStorage([]))
    with pytest.raises(ValueError, match='Connector result must contain payload or result_storage_ref'):
        expand_records_from_response(raw, list_key='accounts', lake_factory=lake)

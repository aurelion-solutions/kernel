# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for reconciliation orchestrator."""

from typing import Any
import uuid

from pydantic import ValidationError
import pytest
from src.capabilities.reconciliation.orchestrator import reconcile_application
from src.platform.applications.exceptions import ApplicationNotFoundError
from src.platform.applications.models import Application
from src.platform.connectors.client import ConnectorClient
from src.platform.connectors.tests.support import (
    HandlerStubRPCClient,
    connector_client_with_stub,
    seed_online_connector_instance,
)


def make_handler_stub(
    accounts: dict | None = None,
    roles: dict | None = None,
    privileges: dict | None = None,
):
    """Handler returning connector result envelopes per operation."""

    async def handler(
        instance_id: str,
        operation: str,
        payload: dict[str, Any],
        _result_storage_requested: bool,
    ) -> dict[str, Any]:
        assert instance_id == 'mock-connector'
        assert 'config' in payload

        if operation == 'list_accounts':
            return {
                'status': 'ok',
                'payload': accounts or {'accounts': []},
            }
        if operation == 'list_roles':
            return {
                'status': 'ok',
                'payload': roles or {'roles': []},
            }
        if operation == 'list_privileges':
            return {
                'status': 'ok',
                'payload': privileges or {'privileges': []},
            }
        raise AssertionError(f'unexpected operation: {operation}')

    return handler


@pytest.mark.asyncio
async def test_accounts_stage_runs_and_returns_result(session_factory):
    """Accounts stage runs and returns result."""
    await seed_online_connector_instance(session_factory)
    stub = HandlerStubRPCClient(
        make_handler_stub(
            accounts={'accounts': [{'identifier': 'u1', 'username': 'alice'}]},
            roles={'roles': []},
            privileges={'privileges': []},
        )
    )
    connector = connector_client_with_stub(stub)

    async with session_factory() as session:
        app = Application(
            name='test-accounts-stage',
            code='test-accounts-stage',
            config={},
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        result = await reconcile_application(session, app_id, connector)
        await session.commit()

    assert result.application_id == str(app_id)
    assert result.accounts.source_total == 1
    assert result.accounts.created == 1
    assert result.roles.source_total == 0
    assert result.privileges.source_total == 0


@pytest.mark.asyncio
async def test_roles_stage_runs_and_returns_result(session_factory):
    """Roles stage runs and returns result."""
    await seed_online_connector_instance(session_factory)
    stub = HandlerStubRPCClient(
        make_handler_stub(
            accounts={'accounts': []},
            roles={'roles': [{'identifier': 'r1', 'name': 'admin'}]},
            privileges={'privileges': []},
        )
    )
    connector = connector_client_with_stub(stub)

    async with session_factory() as session:
        app = Application(
            name='test-roles-stage',
            code='test-roles-stage',
            config={},
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        result = await reconcile_application(session, app_id, connector)
        await session.commit()

    assert result.roles.source_total == 1
    assert result.roles.created == 1
    assert result.accounts.source_total == 0
    assert result.privileges.source_total == 0


@pytest.mark.asyncio
async def test_privileges_stage_runs_and_returns_result(session_factory):
    """Privileges stage runs and returns result."""
    await seed_online_connector_instance(session_factory)
    stub = HandlerStubRPCClient(
        make_handler_stub(
            accounts={'accounts': []},
            roles={'roles': []},
            privileges={'privileges': [{'identifier': 'p1', 'name': 'read'}]},
        )
    )
    connector = connector_client_with_stub(stub)

    async with session_factory() as session:
        app = Application(
            name='test-privileges-stage',
            code='test-privileges-stage',
            config={},
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        result = await reconcile_application(session, app_id, connector)
        await session.commit()

    assert result.privileges.source_total == 1
    assert result.privileges.created == 1
    assert result.accounts.source_total == 0
    assert result.roles.source_total == 0


@pytest.mark.asyncio
async def test_aggregated_result_contains_all_sections(session_factory):
    """Aggregated result contains all sections."""
    await seed_online_connector_instance(session_factory)
    stub = HandlerStubRPCClient(
        make_handler_stub(
            accounts={'accounts': [{'identifier': 'u1', 'username': 'a'}]},
            roles={'roles': [{'identifier': 'r1', 'name': 'admin'}]},
            privileges={'privileges': [{'identifier': 'p1', 'name': 'read'}]},
        )
    )
    connector = connector_client_with_stub(stub)

    async with session_factory() as session:
        app = Application(
            name='test-aggregated',
            code='test-aggregated',
            config={},
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        result = await reconcile_application(session, app_id, connector)
        await session.commit()

    assert result.application_id == str(app_id)
    assert hasattr(result, 'accounts')
    assert hasattr(result, 'roles')
    assert hasattr(result, 'privileges')
    assert result.accounts.source_total == 1
    assert result.roles.source_total == 1
    assert result.privileges.source_total == 1


@pytest.mark.asyncio
async def test_connector_errors_propagate(session_factory):
    """Connector communication errors propagate correctly."""

    class FailingRPCClient:
        async def request(self, **_kwargs: Any) -> Any:
            raise RuntimeError('Connector communication failed')

        def close(self) -> None:
            return None

    await seed_online_connector_instance(session_factory)
    connector = ConnectorClient(
        rpc_client_factory=lambda **kwargs: FailingRPCClient(),
    )

    async with session_factory() as session:
        app = Application(
            name='test-failing-connector',
            code='test-failing-connector',
            config={},
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        with pytest.raises(RuntimeError, match='Connector communication failed'):
            await reconcile_application(session, app_id, connector)


@pytest.mark.asyncio
async def test_dto_validation_errors_stop_execution(session_factory):
    """DTO validation errors stop invalid stage execution."""
    await seed_online_connector_instance(session_factory)
    stub = HandlerStubRPCClient(
        make_handler_stub(
            accounts={'accounts': [{'identifier': ''}]},
            roles={'roles': []},
            privileges={'privileges': []},
        )
    )
    connector = connector_client_with_stub(stub)

    async with session_factory() as session:
        app = Application(
            name='test-dto-validation',
            code='test-dto-validation',
            config={},
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        with pytest.raises(ValidationError):
            await reconcile_application(session, app_id, connector)


@pytest.mark.asyncio
async def test_application_not_found_raises(session_factory):
    """ApplicationNotFoundError raised when application does not exist."""
    stub = HandlerStubRPCClient(make_handler_stub())
    connector = connector_client_with_stub(stub)

    async with session_factory() as session:
        with pytest.raises(ApplicationNotFoundError, match='not found'):
            await reconcile_application(session, uuid.uuid4(), connector)

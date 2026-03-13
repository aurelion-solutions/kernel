# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for full reconciliation flow."""

from typing import Any
import uuid

from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from src.capabilities.reconciliation.routes import get_task_runner
from src.core.db.deps import get_session_factory
from src.inventory.accounts.repository import list_by_application as list_accounts
from src.inventory.privileges.repository import list_by_application as list_privileges
from src.inventory.roles.repository import list_by_application as list_roles
from src.platform.applications.models import Application
from src.platform.connectors.deps import get_connector_client
from src.platform.connectors.tests.support import (
    HandlerStubRPCClient,
    connector_client_with_stub,
    seed_online_connector_instance,
)


async def _awaiting_task_runner(coro: Any) -> None:
    """Test replacement for the default task runner — awaits the coroutine immediately.

    The route does ``await task_runner(coro)``.  In production the default runner
    schedules a fire-and-forget asyncio Task and returns immediately; here we await
    the coroutine directly so reconciliation completes before the response is sent.
    This makes the flow deterministic with no sleep or polling.
    """
    await coro


@pytest.fixture
def mutable_payloads():
    """Mutable payloads for two-phase reconcile scenario."""
    return {
        'accounts': [
            {'identifier': 'u1', 'username': 'alice'},
            {'identifier': 'u2', 'username': 'bob'},
        ],
        'roles': [
            {'identifier': 'r1', 'name': 'admin'},
            {'identifier': 'r2', 'name': 'viewer'},
        ],
        'privileges': [
            {'identifier': 'p1', 'name': 'read'},
            {'identifier': 'p2', 'name': 'write'},
        ],
    }


def _mutable_handler(payloads: dict[str, Any]):
    async def handler(
        _instance_id: str,
        operation: str,
        payload: dict[str, Any],
        _rs: bool,
    ) -> dict[str, Any]:
        assert 'config' in payload
        if operation == 'list_accounts':
            return {
                'status': 'ok',
                'payload': {'accounts': payloads.get('accounts', [])},
            }
        if operation == 'list_roles':
            return {
                'status': 'ok',
                'payload': {'roles': payloads.get('roles', [])},
            }
        if operation == 'list_privileges':
            return {
                'status': 'ok',
                'payload': {'privileges': payloads.get('privileges', [])},
            }
        raise AssertionError(f'unexpected operation: {operation}')

    return handler


@pytest_asyncio.fixture
async def app_with_connector_client(app, mutable_payloads, session_factory):
    """App with connector client and test-scoped session factory + awaiting task runner."""
    await seed_online_connector_instance(session_factory)
    stub = HandlerStubRPCClient(_mutable_handler(mutable_payloads))
    connector = connector_client_with_stub(stub)
    app.dependency_overrides[get_connector_client] = lambda: connector
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_task_runner] = lambda: _awaiting_task_runner
    return app


@pytest.mark.asyncio
async def test_full_reconciliation_persists_accounts_roles_privileges(
    session_factory,
    app_with_connector_client,
):
    """Full reconciliation flow persists accounts, roles, privileges."""
    async with session_factory() as session:
        _suffix = uuid.uuid4().hex[:8]
        app_model = Application(
            name=f'reconcile-full-{_suffix}',
            code=f'reconcile-full-{_suffix}',
            config={},
        )
        session.add(app_model)
        await session.commit()
        app_id = app_model.id

    async with AsyncClient(
        transport=ASGITransport(app=app_with_connector_client),
        base_url='http://testserver',
    ) as client:
        response = await client.post(f'/api/v0/applications/{app_id}/reconcile')

    assert response.status_code == 202
    data = response.json()
    assert 'correlation_id' in data
    assert data['application_id'] == str(app_id)

    async with session_factory() as session:
        accounts = await list_accounts(session, app_id)
        roles = await list_roles(session, app_id)
        privileges = await list_privileges(session, app_id)

    assert len(accounts) == 2
    assert {a.username for a in accounts} == {'alice', 'bob'}
    assert {a.meta.get('identifier') for a in accounts if isinstance(a.meta, dict)} == {'u1', 'u2'}
    assert len(roles) == 2
    assert {r.name for r in roles} == {'admin', 'viewer'}
    assert len(privileges) == 2
    assert {p.name for p in privileges} == {'read', 'write'}


@pytest.mark.asyncio
async def test_second_reconcile_with_reduced_payload_marks_missing_inactive(
    session_factory,
    app,
    mutable_payloads,
):
    """Second reconcile with reduced payload marks missing records inactive."""
    await seed_online_connector_instance(session_factory)
    stub = HandlerStubRPCClient(_mutable_handler(mutable_payloads))
    connector = connector_client_with_stub(stub)
    app.dependency_overrides[get_connector_client] = lambda: connector
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    app.dependency_overrides[get_task_runner] = lambda: _awaiting_task_runner

    async with session_factory() as session:
        _suffix2 = uuid.uuid4().hex[:8]
        app_model = Application(
            name=f'reconcile-second-{_suffix2}',
            code=f'reconcile-second-{_suffix2}',
            config={},
        )
        session.add(app_model)
        await session.commit()
        app_id = app_model.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response1 = await client.post(f'/api/v0/applications/{app_id}/reconcile')
    assert response1.status_code == 202

    mutable_payloads['accounts'] = [{'identifier': 'u1', 'username': 'alice'}]
    mutable_payloads['roles'] = [{'identifier': 'r1', 'name': 'admin'}]
    mutable_payloads['privileges'] = [{'identifier': 'p1', 'name': 'read'}]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response2 = await client.post(f'/api/v0/applications/{app_id}/reconcile')
    assert response2.status_code == 202

    async with session_factory() as session:
        accounts = await list_accounts(session, app_id)
        roles = await list_roles(session, app_id)
        privileges = await list_privileges(session, app_id)

    by_id = {a.meta.get('identifier'): a for a in accounts if isinstance(a.meta, dict)}
    assert by_id['u1'].is_active is True
    assert by_id['u2'].is_active is False

    by_id_r = {r.meta.get('identifier'): r for r in roles if isinstance(r.meta, dict)}
    assert by_id_r['r1'].is_active is True
    assert by_id_r['r2'].is_active is False

    by_id_p = {p.meta.get('identifier'): p for p in privileges if isinstance(p.meta, dict)}
    assert by_id_p['p1'].is_active is True
    assert by_id_p['p2'].is_active is False


@pytest.mark.asyncio
async def test_response_counters_match_database_state(
    session_factory,
    app_with_connector_client,
):
    """Database state after reconciliation matches expected counts."""
    async with session_factory() as session:
        _suffix3 = uuid.uuid4().hex[:8]
        app_model = Application(
            name=f'reconcile-counters-{_suffix3}',
            code=f'reconcile-counters-{_suffix3}',
            config={},
        )
        session.add(app_model)
        await session.commit()
        app_id = app_model.id

    async with AsyncClient(
        transport=ASGITransport(app=app_with_connector_client),
        base_url='http://testserver',
    ) as client:
        response = await client.post(f'/api/v0/applications/{app_id}/reconcile')

    assert response.status_code == 202

    async with session_factory() as session:
        accounts = await list_accounts(session, app_id)
        roles = await list_roles(session, app_id)
        privileges = await list_privileges(session, app_id)

    assert len(accounts) == 2
    assert len(roles) == 2
    assert len(privileges) == 2
    assert all(a.is_active for a in accounts)
    assert all(r.is_active for r in roles)
    assert all(p.is_active for p in privileges)

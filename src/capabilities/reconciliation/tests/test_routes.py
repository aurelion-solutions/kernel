# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP tests for ``capabilities.reconciliation.routes``."""

import asyncio
from typing import Any
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.capabilities.reconciliation.routes import router as reconciliation_router
from src.core.db.deps import get_db
from src.platform.applications.models import Application
from src.platform.applications.routes import router as applications_router
from src.platform.connectors.deps import get_connector_client
from src.platform.connectors.tests.support import (
    HandlerStubRPCClient,
    connector_client_with_stub,
    seed_online_connector_instance,
)
from src.platform.storage.factory import data_lake_factory


class DummyConnectorClient:
    lake_factory = data_lake_factory

    async def invoke(
        self,
        _instance_id,
        operation,
        payload,
        *,
        result_storage_requested=False,
        **_kwargs,
    ):
        assert 'config' in payload
        if operation == 'list_accounts':
            return {'status': 'ok', 'payload': {'accounts': []}}
        if operation == 'list_roles':
            return {'status': 'ok', 'payload': {'roles': []}}
        if operation == 'list_privileges':
            return {'status': 'ok', 'payload': {'privileges': []}}
        raise AssertionError(operation)


@pytest.mark.asyncio
async def test_reconcile_returns_404_for_unknown_application(session_factory):
    async def override_get_db():
        async with session_factory() as session:
            yield session

    test_app = FastAPI()
    test_app.include_router(applications_router, prefix='/api/v0')
    test_app.include_router(reconciliation_router, prefix='/api/v0')
    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_connector_client] = lambda: DummyConnectorClient()

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(f'/api/v0/applications/{uuid.uuid4()}/reconcile')

    assert response.status_code == 404
    assert response.json() == {'detail': 'Application not found'}


@pytest.mark.asyncio
async def test_reconcile_returns_409_when_no_connector_instance_matches(session_factory):
    """409 when no online connector instance matches application tags (client never invoked)."""
    async with session_factory() as session:
        application = Application(
            name='reconcile-route-app-3',
            code='reconcile-route-app-3',
            required_connector_tags=['jira', 'eu-segment'],
        )
        session.add(application)
        await session.commit()
        await session.refresh(application)
        application_id = application.id

    async def override_get_db():
        async with session_factory() as session:
            yield session

    test_app = FastAPI()
    test_app.include_router(applications_router, prefix='/api/v0')
    test_app.include_router(reconciliation_router, prefix='/api/v0')
    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_connector_client] = lambda: DummyConnectorClient()

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(f'/api/v0/applications/{application_id}/reconcile')

    assert response.status_code == 409
    assert 'No connector instance found' in response.json()['detail']


@pytest.mark.asyncio
async def test_reconcile_returns_202_quickly_while_job_runs_slow_connector(
    session_factory,
) -> None:
    """POST does not wait for full reconciliation (slow connector invokes)."""
    await seed_online_connector_instance(session_factory)

    async def slow_handler(
        _instance_id: str,
        operation: str,
        payload: dict[str, Any],
        _result_storage_requested: bool,
    ) -> dict[str, Any]:
        await asyncio.sleep(2.0)
        assert 'config' in payload
        if operation == 'list_accounts':
            return {'status': 'ok', 'payload': {'accounts': []}}
        if operation == 'list_roles':
            return {'status': 'ok', 'payload': {'roles': []}}
        if operation == 'list_privileges':
            return {'status': 'ok', 'payload': {'privileges': []}}
        raise AssertionError(operation)

    slow_client = connector_client_with_stub(HandlerStubRPCClient(slow_handler))

    async with session_factory() as session:
        application = Application(
            name='reconcile-async-slow',
            code='reconcile-async-slow',
            required_connector_tags=[],
        )
        session.add(application)
        await session.commit()
        await session.refresh(application)
        application_id = application.id

    async def override_get_db():
        async with session_factory() as session:
            yield session

    test_app = FastAPI()
    test_app.include_router(applications_router, prefix='/api/v0')
    test_app.include_router(reconciliation_router, prefix='/api/v0')
    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_connector_client] = lambda: slow_client

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
        timeout=0.35,
    ) as client:
        response = await client.post(
            f'/api/v0/applications/{application_id}/reconcile',
        )

    assert response.status_code == 202
    body = response.json()
    assert 'correlation_id' in body
    assert body['application_id'] == str(application_id)

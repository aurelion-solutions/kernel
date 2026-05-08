# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP tests for ``engines.provisioning.routes``."""

import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.core.db.deps import get_db
from src.engines.provisioning.routes import router as provisioning_router
from src.platform.applications.models import Application
from src.platform.applications.routes import router as applications_router
from src.platform.connectors.deps import get_connector_client
from src.platform.connectors.service import ConnectorInstanceService


class DummyConnectorClient:
    async def invoke(
        self,
        _instance_id,
        operation,
        payload,
        *,
        result_storage_requested=False,
        **_kwargs,
    ):
        return {'status': 'ok', 'payload': {}}


@pytest.mark.asyncio
async def test_create_account_returns_201(session_factory):
    async with session_factory() as session:
        connector_service = ConnectorInstanceService()
        await connector_service.upsert_instance(
            session,
            instance_id='conn-inst-provision-1',
            tags=['jira', 'eu-segment'],
        )
        application = Application(
            name='provision-route-app-1',
            code='provision-route-app-1',
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
    test_app.include_router(provisioning_router, prefix='/api/v0')
    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_connector_client] = lambda: DummyConnectorClient()

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            f'/api/v0/applications/{application_id}/accounts',
            json={'username': 'alice', 'email': 'alice@example.org'},
        )

    assert response.status_code == 201
    assert response.json() == {
        'username': 'alice',
        'email': 'alice@example.org',
        'status': 'accepted',
    }


@pytest.mark.asyncio
async def test_create_account_returns_404_for_unknown_application(session_factory):
    async def override_get_db():
        async with session_factory() as session:
            yield session

    test_app = FastAPI()
    test_app.include_router(applications_router, prefix='/api/v0')
    test_app.include_router(provisioning_router, prefix='/api/v0')
    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_connector_client] = lambda: DummyConnectorClient()

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            f'/api/v0/applications/{uuid.uuid4()}/accounts',
            json={'username': 'alice', 'email': 'alice@example.org'},
        )

    assert response.status_code == 404
    assert response.json() == {'detail': 'Application not found'}


@pytest.mark.asyncio
async def test_create_account_returns_409_when_no_connector_instance_matches(
    session_factory,
):
    """409 when no online connector instance matches application tags (client never invoked)."""
    async with session_factory() as session:
        application = Application(
            name='provision-route-app-2',
            code='provision-route-app-2',
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
    test_app.include_router(provisioning_router, prefix='/api/v0')
    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_connector_client] = lambda: DummyConnectorClient()

    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            f'/api/v0/applications/{application_id}/accounts',
            json={'username': 'alice', 'email': 'alice@example.org'},
        )

    assert response.status_code == 409
    assert 'No connector instance found' in response.json()['detail']

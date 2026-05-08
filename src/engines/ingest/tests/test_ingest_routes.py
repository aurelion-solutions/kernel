# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for POST /connector-results."""

import uuid

from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select
from src.engines.ingest.models import StagingConnectorResult


def _base_payload(
    app_id: str,
    task_id: str | None = None,
    result_id: str | None = None,
    result_type: str = 'inline',
):
    return {
        'task_id': task_id or str(uuid.uuid4()),
        'application_id': app_id,
        'operation': 'reconcile',
        'status': 'completed',
        'result_type': result_type,
        'result_id': result_id or str(uuid.uuid4()),
    }


@pytest.mark.asyncio
async def test_ingest_generic_command_result_only(app):
    """Ingest generic command result only (payload, no accounts/resources)."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/applications',
            json={
                'name': 'my-app',
                'code': 'my-app',
                'config': {},
            },
        )
    assert create_resp.status_code == 201
    app_id = create_resp.json()['id']

    task_id = str(uuid.uuid4())
    result_id = str(uuid.uuid4())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/api/v0/connector-results',
            json={
                **_base_payload(app_id, task_id, result_id),
                'payload': {'account_id': 'ext-123', 'action': 'created'},
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data['task_id'] == task_id
    assert data['result_id'] == result_id
    assert data['operation'] == 'reconcile'
    assert data['status'] == 'completed'


@pytest.mark.asyncio
async def test_ingest_invalid_request_returns_422(app):
    """Invalid request (inline without payload) returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/api/v0/connector-results',
            json={
                **_base_payload(str(uuid.uuid4())),
            },
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_inline_result_accepted(app):
    """Inline result with payload accepted."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/applications',
            json={
                'name': 'my-app',
                'code': 'my-app',
                'config': {},
            },
        )
    assert create_resp.status_code == 201
    app_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/api/v0/connector-results',
            json={
                **_base_payload(app_id, result_type='inline'),
                'payload': {'action': 'synced', 'count': 3},
            },
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ingest_lake_ref_result_accepted(app):
    """Lake_ref result with location accepted."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/applications',
            json={
                'name': 'my-app',
                'code': 'my-app',
                'config': {},
            },
        )
    assert create_resp.status_code == 201
    app_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/api/v0/connector-results',
            json={
                **_base_payload(app_id, result_type='lake_ref'),
                'location': {'provider': 'file', 'storage_key': 'accounts/batch-1'},
            },
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ingest_inline_without_payload_rejected(app):
    """Inline result without payload returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/applications',
            json={
                'name': 'my-app',
                'code': 'my-app',
                'config': {},
            },
        )
    assert create_resp.status_code == 201
    app_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/api/v0/connector-results',
            json={**_base_payload(app_id, result_type='inline')},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_lake_ref_without_location_rejected(app):
    """Lake_ref result without location returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/applications',
            json={
                'name': 'my-app',
                'code': 'my-app',
                'config': {},
            },
        )
    assert create_resp.status_code == 201
    app_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/api/v0/connector-results',
            json={**_base_payload(app_id, result_type='lake_ref')},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_invalid_result_type_rejected(app):
    """Invalid result_type returns 422."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/applications',
            json={
                'name': 'my-app',
                'code': 'my-app',
                'config': {},
            },
        )
    assert create_resp.status_code == 201
    app_id = create_resp.json()['id']

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/api/v0/connector-results',
            json={
                **_base_payload(app_id),
                'result_type': 'unknown',
                'payload': {'x': 1},
            },
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_ingest_rows_linked_by_task_id_and_result_id(app, session_factory):
    """Inserted rows have correct task_id and result_id."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        create_resp = await client.post(
            '/api/v0/applications',
            json={
                'name': 'my-app',
                'code': 'my-app',
                'config': {},
            },
        )
    assert create_resp.status_code == 201
    app_id = create_resp.json()['id']

    task_id = uuid.uuid4()
    result_id = uuid.uuid4()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/api/v0/connector-results',
            json={
                **_base_payload(app_id, str(task_id), str(result_id)),
                'payload': {'e1': 'a1', 'raw_data': {'x': 1}},
            },
        )
    assert resp.status_code == 200

    async with session_factory() as session:
        conn_results = (
            (
                await session.execute(
                    select(StagingConnectorResult).where(
                        StagingConnectorResult.task_id == task_id,
                        StagingConnectorResult.result_id == result_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    assert len(conn_results) == 1
    assert conn_results[0].operation == 'reconcile'
    assert conn_results[0].status == 'completed'
    assert conn_results[0].payload == {'e1': 'a1', 'raw_data': {'x': 1}}


@pytest.mark.asyncio
async def test_ingest_unknown_application_returns_404(app):
    """Ingest for non-existent application returns 404."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        resp = await client.post(
            '/api/v0/connector-results',
            json={
                **_base_payload(str(uuid.uuid4())),
                'payload': {'x': 1},
            },
        )

    assert resp.status_code == 404

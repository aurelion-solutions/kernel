# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP tests for ``engines.policy_assessment.routes``."""

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.engines.policy_assessment.routes import router as policy_router


def make_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(policy_router, prefix='/api/v0')
    return app


NOW = '2026-04-13T12:00:00Z'


@pytest.mark.asyncio
async def test_evaluate_terminated_employee() -> None:
    """Terminated employee → abstract_state=disabled, concrete_state from AD mapping."""
    app = make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/policy/evaluate',
            json={
                'subject': {'id': 'emp-1', 'kind': 'employee', 'status': 'terminated'},
                'target': {
                    'application': 'ad',
                    'initiatives': [{'type': 'birthright', 'origin': 'hris'}],
                },
                'now': NOW,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body['abstract_state'] == 'disabled'
    assert isinstance(body['concrete_state'], str)


@pytest.mark.asyncio
async def test_evaluate_active_employee() -> None:
    """Active employee with requested initiative → abstract_state=enabled."""
    app = make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/policy/evaluate',
            json={
                'subject': {'id': 'emp-2', 'kind': 'employee', 'status': 'active'},
                'target': {
                    'application': 'jira',
                    'initiatives': [{'type': 'requested', 'origin': 'itsm'}],
                },
                'now': NOW,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body['abstract_state'] == 'enabled'


@pytest.mark.asyncio
async def test_evaluate_nhi_owner_terminated() -> None:
    """NHI whose owner is terminated → abstract_state=disabled."""
    app = make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/policy/evaluate',
            json={
                'subject': {
                    'id': 'nhi-1',
                    'kind': 'nhi',
                    'status': 'active',
                    'owner': {'id': 'emp-3', 'status': 'terminated'},
                },
                'target': {'application': 'github'},
                'now': NOW,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body['abstract_state'] == 'disabled'


@pytest.mark.asyncio
async def test_evaluate_customer_banned() -> None:
    """Banned customer → abstract_state=disabled, revoke_all_sessions + purge_api_keys."""
    app = make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/policy/evaluate',
            json={
                'subject': {'id': 'cust-1', 'kind': 'customer', 'status': 'banned'},
                'target': {
                    'application': 'stripe_billing',
                    'initiatives': [{'type': 'subscription', 'origin': 'billing'}],
                },
                'now': NOW,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body['abstract_state'] == 'disabled'
    assert 'revoke_all_sessions' in body['actions']
    assert 'purge_api_keys' in body['actions']


@pytest.mark.asyncio
async def test_evaluate_idp_target_none() -> None:
    """Terminated employee with target=null (IDP) → disabled, concrete_state is null."""
    app = make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/policy/evaluate',
            json={
                'subject': {'id': 'emp-4', 'kind': 'employee', 'status': 'terminated'},
                'target': None,
                'now': NOW,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body['abstract_state'] == 'disabled'
    assert body['concrete_state'] is None


@pytest.mark.asyncio
async def test_evaluate_risk_credential_compromised() -> None:
    """Active employee with credential_compromised indicator → risk_level=critical."""
    app = make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/policy/evaluate',
            json={
                'subject': {'id': 'emp-5', 'kind': 'employee', 'status': 'active'},
                'target': {'application': 'ad'},
                'threat': {'active_indicators': ['credential_compromised']},
                'now': NOW,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body['risk_level'] == 'critical'


@pytest.mark.asyncio
async def test_evaluate_customer_idp_banned() -> None:
    """Banned customer with target=null → 200, response has all required Decision fields."""
    app = make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/policy/evaluate',
            json={
                'subject': {'id': 'cust-2', 'kind': 'customer', 'status': 'banned'},
                'target': None,
                'now': NOW,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert 'abstract_state' in body
    assert 'actions' in body
    assert 'signals' in body
    assert 'reasons' in body
    # concrete_state is null when target is None (no application mapping)
    assert body['concrete_state'] is None


@pytest.mark.asyncio
async def test_evaluate_invalid_body_returns_422() -> None:
    """Empty body → 422 (subject and now are required)."""
    app = make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post('/api/v0/policy/evaluate', json={})

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_evaluate_missing_subject_fields_returns_422() -> None:
    """Empty subject object → 422 (id, kind, status are required on SubjectFacts)."""
    app = make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/policy/evaluate',
            json={'subject': {}, 'now': NOW},
        )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_evaluate_response_contains_reasons() -> None:
    """Terminated employee → reasons list is non-empty with required fields."""
    app = make_test_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as client:
        response = await client.post(
            '/api/v0/policy/evaluate',
            json={
                'subject': {'id': 'emp-6', 'kind': 'employee', 'status': 'terminated'},
                'target': {'application': 'ad'},
                'now': NOW,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert len(body['reasons']) > 0
    reason = body['reasons'][0]
    assert 'rule_id' in reason
    assert 'precedence' in reason
    assert 'matched_conditions' in reason

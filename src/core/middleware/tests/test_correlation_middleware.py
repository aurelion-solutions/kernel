# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for CorrelationIdMiddleware."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.core.context import current_correlation_id
from src.core.middleware.correlation import CORRELATION_HEADER, CorrelationIdMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get('/echo')
    async def echo() -> dict:
        return {'correlation_id': current_correlation_id()}

    return app


@pytest.fixture()
def app() -> FastAPI:
    return _build_app()


@pytest.mark.asyncio
async def test_header_echoed_when_provided(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/echo', headers={CORRELATION_HEADER: 'abc-123'})

    assert response.status_code == 200
    assert response.headers[CORRELATION_HEADER] == 'abc-123'
    assert response.json()['correlation_id'] == 'abc-123'


@pytest.mark.asyncio
async def test_header_generated_when_absent(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/echo')

    assert response.status_code == 200
    generated = response.headers[CORRELATION_HEADER]
    assert generated  # non-empty
    # parseable as UUID
    parsed = uuid.UUID(generated)
    assert str(parsed) == generated
    assert response.json()['correlation_id'] == generated


@pytest.mark.asyncio
async def test_empty_header_treated_as_absent(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/echo', headers={CORRELATION_HEADER: '   '})

    assert response.status_code == 200
    generated = response.headers[CORRELATION_HEADER]
    assert generated
    # must be a valid UUID (freshly generated, not the whitespace value)
    parsed = uuid.UUID(generated)
    assert str(parsed) == generated


@pytest.mark.asyncio
async def test_contextvar_does_not_leak_between_requests(app: FastAPI) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        r1 = await client.get('/echo')
        r2 = await client.get('/echo')

    cid1 = r1.headers[CORRELATION_HEADER]
    cid2 = r2.headers[CORRELATION_HEADER]
    assert cid1 != cid2
    assert r1.json()['correlation_id'] == cid1
    assert r2.json()['correlation_id'] == cid2
    # ContextVar must be reset after each request — if reset(token) is removed, this fails.
    assert current_correlation_id() is None

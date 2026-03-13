# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for log read API routes."""

import json
import os
from pathlib import Path

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from src.platform.logs.routes import router as logs_router


@pytest.fixture
def logs_app() -> FastAPI:
    """Minimal app with logs router."""
    app = FastAPI()
    app.include_router(logs_router, prefix='/api/v0')
    return app


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    """Temporary log file path."""
    return tmp_path / 'test.log.jsonl'


@pytest.mark.asyncio
async def test_get_logs_file_returns_recent_records(logs_app, log_file: Path) -> None:
    """GET /api/v0/logs returns recent log records as JSON array for file provider."""
    log_file.write_text('{"event_type":"test","level":"info","message":"hi"}\n')
    env_before = os.environ.copy()
    try:
        os.environ['AURELION_LOG_PROVIDER'] = 'file'
        os.environ['AURELION_LOG_FILE_PATH'] = str(log_file)
        async with AsyncClient(
            transport=ASGITransport(app=logs_app),
            base_url='http://testserver',
        ) as client:
            response = await client.get('/api/v0/logs')
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]['event_type'] == 'test'
        assert data[0]['message'] == 'hi'
    finally:
        os.environ.clear()
        os.environ.update(env_before)


@pytest.mark.asyncio
async def test_get_logs_respects_limit(logs_app, log_file: Path) -> None:
    """GET /api/v0/logs?limit=N respects limit."""
    lines = [json.dumps({'n': i}) + '\n' for i in range(20)]
    log_file.write_text(''.join(lines))
    env_before = os.environ.copy()
    try:
        os.environ['AURELION_LOG_PROVIDER'] = 'file'
        os.environ['AURELION_LOG_FILE_PATH'] = str(log_file)
        async with AsyncClient(
            transport=ASGITransport(app=logs_app),
            base_url='http://testserver',
        ) as client:
            response = await client.get('/api/v0/logs', params={'limit': 5})
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 5
        assert [r['n'] for r in data] == [15, 16, 17, 18, 19]
    finally:
        os.environ.clear()
        os.environ.update(env_before)


@pytest.mark.asyncio
async def test_get_logs_missing_file_returns_empty_array(logs_app, log_file: Path) -> None:
    """When log file does not exist, GET /logs returns empty array for file provider."""
    assert not log_file.exists()
    env_before = os.environ.copy()
    try:
        os.environ['AURELION_LOG_PROVIDER'] = 'file'
        os.environ['AURELION_LOG_FILE_PATH'] = str(log_file)
        async with AsyncClient(
            transport=ASGITransport(app=logs_app),
            base_url='http://testserver',
        ) as client:
            response = await client.get('/api/v0/logs')
        assert response.status_code == 200
        assert response.json() == []
    finally:
        os.environ.clear()
        os.environ.update(env_before)


@pytest.mark.asyncio
async def test_get_logs_stub_provider_returns_501(logs_app) -> None:
    """When provider read is stub, GET /logs returns 501."""
    env_before = os.environ.copy()
    try:
        os.environ['AURELION_LOG_PROVIDER'] = 'elk'
        async with AsyncClient(
            transport=ASGITransport(app=logs_app),
            base_url='http://testserver',
        ) as client:
            response = await client.get('/api/v0/logs')
        assert response.status_code == 501
    finally:
        os.environ.clear()
        os.environ.update(env_before)


@pytest.mark.asyncio
async def test_get_logs_invalid_limit_returns_400(logs_app, log_file: Path) -> None:
    """GET /api/v0/logs with invalid limit returns 400."""
    env_before = os.environ.copy()
    try:
        os.environ['AURELION_LOG_PROVIDER'] = 'file'
        os.environ['AURELION_LOG_FILE_PATH'] = str(log_file)
        async with AsyncClient(
            transport=ASGITransport(app=logs_app),
            base_url='http://testserver',
        ) as client:
            response = await client.get('/api/v0/logs', params={'limit': 0})
        assert response.status_code == 400
    finally:
        os.environ.clear()
        os.environ.update(env_before)

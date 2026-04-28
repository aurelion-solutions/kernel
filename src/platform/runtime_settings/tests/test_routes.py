# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API tests for /runtime-settings endpoints."""

from __future__ import annotations

import pytest
from src.platform.logs.service import NoOpLogService
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig
from src.platform.runtime_settings.service import RuntimeSettingsService


@pytest.mark.asyncio
async def test_get_list_returns_all_settings(client, session_factory) -> None:
    """GET /runtime-settings returns all seeded settings."""
    # Seed defaults
    async with session_factory() as session:
        svc = RuntimeSettingsService(session, NoOpLogService())
        await svc.ensure_defaults()
        await session.commit()

    resp = await client.get('/api/v0/runtime-settings')
    assert resp.status_code == 200
    data = resp.json()
    expected_count = len(RuntimeSettingsConfig.model_fields)
    assert len(data) == expected_count
    keys = {item['key'] for item in data}
    assert 'lake_pool_size' in keys
    assert 'llm_max_messages' in keys


@pytest.mark.asyncio
async def test_get_single_returns_setting(client, session_factory) -> None:
    """GET /runtime-settings/{key} returns a single setting."""
    async with session_factory() as session:
        svc = RuntimeSettingsService(session, NoOpLogService())
        await svc.ensure_defaults()
        await session.commit()

    resp = await client.get('/api/v0/runtime-settings/lake_pool_size')
    assert resp.status_code == 200
    data = resp.json()
    assert data['key'] == 'lake_pool_size'
    assert data['value'] == '4'
    assert data['value_type'] == 'int'


@pytest.mark.asyncio
async def test_get_missing_returns_404(client) -> None:
    """GET /runtime-settings/{key} returns 404 for unknown key."""
    resp = await client.get('/api/v0/runtime-settings/nonexistent_key')
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_update_returns_200(client, session_factory) -> None:
    """PUT /runtime-settings/{key} updates the value and returns 200."""
    async with session_factory() as session:
        svc = RuntimeSettingsService(session, NoOpLogService())
        await svc.ensure_defaults()
        await session.commit()

    resp = await client.put(
        '/api/v0/runtime-settings/lake_pool_size',
        json={'value': '8'},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data['value'] == '8'


@pytest.mark.asyncio
async def test_put_missing_key_returns_404(client) -> None:
    """PUT /runtime-settings/{key} returns 404 for unknown key."""
    resp = await client.put(
        '/api/v0/runtime-settings/nonexistent_key',
        json={'value': 'x'},
    )
    assert resp.status_code == 404

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service tests for RuntimeSettingsService."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from src.platform.logs.service import NoOpLogService
from src.platform.runtime_settings.models import RuntimeSetting
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig, RuntimeSettingUpdate
from src.platform.runtime_settings.service import InvalidRuntimeSettingValueError, RuntimeSettingsService


def _svc(session) -> RuntimeSettingsService:
    return RuntimeSettingsService(session, NoOpLogService())


@pytest.mark.asyncio
async def test_ensure_defaults_inserts_all_known_keys(session_factory) -> None:
    """ensure_defaults() inserts one row per known key on a cold start."""
    async with session_factory() as session:
        svc = _svc(session)
        inserted = await svc.ensure_defaults()
        await session.commit()

    expected = len(RuntimeSettingsConfig.model_fields)
    assert inserted == expected

    async with session_factory() as session:
        result = await session.execute(select(RuntimeSetting))
        rows = list(result.scalars())
    assert len(rows) == expected


@pytest.mark.asyncio
async def test_ensure_defaults_is_idempotent(session_factory) -> None:
    """Calling ensure_defaults() twice does not duplicate rows."""
    async with session_factory() as session:
        svc = _svc(session)
        first = await svc.ensure_defaults()
        await session.commit()

    async with session_factory() as session:
        svc = _svc(session)
        second = await svc.ensure_defaults()
        await session.commit()

    # First run seeds all keys; second run inserts nothing.
    assert first > 0
    assert second == 0

    async with session_factory() as session:
        result = await session.execute(select(RuntimeSetting))
        rows = list(result.scalars())

    assert len(rows) == first  # row count unchanged


@pytest.mark.asyncio
async def test_load_returns_defaults_when_db_empty(session_factory) -> None:
    """load() falls back to typed defaults when no rows exist."""
    async with session_factory() as session:
        svc = _svc(session)
        config = await svc.load()

    defaults = RuntimeSettingsConfig()
    assert config == defaults


@pytest.mark.asyncio
async def test_load_uses_db_values(session_factory) -> None:
    """load() reflects values written to the DB."""
    async with session_factory() as session:
        svc = _svc(session)
        await svc.ensure_defaults()
        await session.commit()

    # Update a value directly
    async with session_factory() as session:
        svc = _svc(session)
        await svc.update('lake_pool_size', RuntimeSettingUpdate(value='8'))
        await session.commit()

    async with session_factory() as session:
        svc = _svc(session)
        config = await svc.load()

    assert config.lake_pool_size == 8


@pytest.mark.asyncio
async def test_update_emits_one_log_event(session_factory) -> None:
    """update() calls log_service.emit_safe exactly once."""
    from unittest.mock import MagicMock

    fake_log = MagicMock()
    fake_log.emit_safe = MagicMock()

    async with session_factory() as session:
        # Seed the row first
        svc = RuntimeSettingsService(session, NoOpLogService())
        await svc.ensure_defaults()
        await session.commit()

    async with session_factory() as session:
        svc = RuntimeSettingsService(session, fake_log)
        await svc.update('app_name', RuntimeSettingUpdate(value='MyApp'))
        await session.commit()

    fake_log.emit_safe.assert_called_once()
    call_kwargs = fake_log.emit_safe.call_args.kwargs
    assert call_kwargs['message'] == 'runtime_setting.updated'
    assert call_kwargs['payload']['key'] == 'app_name'
    assert call_kwargs['payload']['new_value'] == 'MyApp'


@pytest.mark.asyncio
async def test_update_raises_key_error_for_unknown_key(session_factory) -> None:
    """update() raises KeyError when the key does not exist."""
    async with session_factory() as session:
        svc = _svc(session)
        with pytest.raises(KeyError):
            await svc.update('nonexistent_key', RuntimeSettingUpdate(value='x'))


@pytest.mark.asyncio
async def test_update_rejects_non_numeric_value_for_int_field(session_factory) -> None:
    """update() raises InvalidRuntimeSettingValueError when value cannot be coerced."""
    async with session_factory() as session:
        svc = _svc(session)
        await svc.ensure_defaults()
        await session.commit()

    async with session_factory() as session:
        svc = _svc(session)
        with pytest.raises(InvalidRuntimeSettingValueError):
            await svc.update('lake_pool_size', RuntimeSettingUpdate(value='abc'))


@pytest.mark.asyncio
async def test_update_rejects_value_violating_constraints(session_factory) -> None:
    """update() raises InvalidRuntimeSettingValueError when value fails Pydantic constraints."""
    async with session_factory() as session:
        svc = _svc(session)
        await svc.ensure_defaults()
        await session.commit()

    async with session_factory() as session:
        svc = _svc(session)
        # lake_read_page_size has le=5000
        with pytest.raises(InvalidRuntimeSettingValueError):
            await svc.update('lake_read_page_size', RuntimeSettingUpdate(value='9999'))

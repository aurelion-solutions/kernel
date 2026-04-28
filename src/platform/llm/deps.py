# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for the LLM platform slice."""

from __future__ import annotations

import functools

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.platform.llm.factory import LLMFactory
from src.platform.logs.service import NoOpLogService
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig
from src.platform.runtime_settings.service import RuntimeSettingsService


@functools.lru_cache(maxsize=1)
def get_llm_factory() -> LLMFactory:
    """Return the process-singleton LLMFactory instance.

    ``lru_cache(maxsize=1)`` ensures a single ``LLMFactory`` is constructed per
    process lifetime, preserving the in-process LRU provider cache across
    requests.

    Note: ``llm_max_loaded_models`` is read once from ``RuntimeSettingsConfig``
    defaults at startup.  Changes to this knob via PUT /runtime-settings require
    a process restart to take effect.  A live-reload protocol is planned for a
    future phase.

    Tests that need a fake factory MUST use::

        app.dependency_overrides[get_llm_factory] = lambda: fake_factory

    and clear the override in teardown.  Never mutate the lru_cache directly.
    """
    defaults = RuntimeSettingsConfig()
    return LLMFactory(max_loaded_models=defaults.llm_max_loaded_models)


async def get_runtime_settings(
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> RuntimeSettingsConfig:
    """Read the live RuntimeSettingsConfig snapshot from the database.

    Falls back to typed defaults for any key not yet seeded.
    Injected into inference handlers so that knob changes (e.g.
    ``llm_max_messages``) take effect on the next request without restart.
    """
    service = RuntimeSettingsService(session, NoOpLogService())
    return await service.load()

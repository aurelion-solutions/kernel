# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for the LLM platform slice."""

from __future__ import annotations

import functools

from src.platform.llm.factory import LLMFactory
from src.platform.llm.settings import LLMSettings


@functools.lru_cache(maxsize=1)
def get_llm_factory() -> LLMFactory:
    """Return the process-singleton LLMFactory instance.

    ``lru_cache(maxsize=1)`` ensures:
    - A single ``LLMFactory`` is constructed per process lifetime, preserving
      the in-process LRU provider cache across requests.
    - ``app.dependency_overrides[get_llm_factory] = ...`` works correctly in
      tests because FastAPI matches the stable function object as the key.

    Tests that need a fake factory MUST use::

        app.dependency_overrides[get_llm_factory] = lambda: fake_factory

    and clear the override in teardown.  Never mutate the lru_cache directly.
    """
    return LLMFactory(settings=LLMSettings())

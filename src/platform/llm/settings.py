# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Operator-tunable LLM knobs for the LLM platform slice.

Mirrors the secrets / storage pattern: slice-local ``LLMSettings`` is
intentionally NOT a nested field on the kernel-wide ``Settings``.  The
module-level singleton ``llm_settings`` is instantiated here so that
``__init__.py`` re-exports it without inverting the module → infra flow.

All four fields are available at import time so downstream steps (Step 10
``LLMService``) can read them without touching this file again.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """Pydantic-settings v2 class for LLM slice configuration.

    Environment variables (case-insensitive):
    - ``LLM_MAX_LOADED_MODELS``    — max providers in the LRU cache (default 2).
    - ``LLM_MAX_MESSAGES``         — max conversation turns per request (default 32).
    - ``LLM_MAX_CHARS_PER_MESSAGE`` — max characters in a single message (default 32_000).
    - ``LLM_MAX_TOTAL_CHARS``      — max total characters across all messages (default 128_000).
    """

    max_loaded_models: int = Field(default=2, ge=1)
    max_messages: int = Field(default=32, ge=1)
    max_chars_per_message: int = Field(default=32_000, ge=1)
    max_total_chars: int = Field(default=128_000, ge=1)

    model_config = SettingsConfigDict(env_prefix='LLM_', extra='ignore')


llm_settings = LLMSettings()

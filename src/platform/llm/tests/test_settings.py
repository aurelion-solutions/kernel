# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for LLM runtime settings (now part of RuntimeSettingsConfig).

Pure unit tests — no DB, no async.
"""

from pydantic import ValidationError
import pytest
from src.platform.llm.factory import LLMFactory
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

# ---------------------------------------------------------------------------
# RuntimeSettingsConfig defaults for LLM fields
# ---------------------------------------------------------------------------


def test_llm_defaults_in_runtime_settings_config() -> None:
    c = RuntimeSettingsConfig()
    assert c.llm_max_loaded_models == 2
    assert c.llm_max_messages == 32
    assert c.llm_max_chars_per_message == 32_000
    assert c.llm_max_total_chars == 128_000


def test_rejects_zero_max_loaded_models() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettingsConfig(llm_max_loaded_models=0)


def test_rejects_negative_max_messages() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettingsConfig(llm_max_messages=-1)


# ---------------------------------------------------------------------------
# Factory-wiring tests
# ---------------------------------------------------------------------------


def test_factory_uses_explicit_max_loaded_models() -> None:
    factory = LLMFactory(max_loaded_models=7)
    assert factory._max_loaded_models == 7


def test_factory_explicit_max_loaded_models_overrides_settings() -> None:
    factory = LLMFactory(max_loaded_models=3)
    assert factory._max_loaded_models == 3


def test_factory_defaults_to_2_when_no_args_given() -> None:
    """Factory falls back to settings=None path which uses RuntimeSettingsConfig defaults."""
    factory = LLMFactory()
    assert factory._max_loaded_models == 2

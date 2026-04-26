# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for LLMSettings and its wiring into LLMFactory.

Pure unit tests — no DB, no async, no monkeypatched providers.
Each test constructs a fresh LLMSettings() to bypass the module-level
singleton's import-time freeze.
"""

from pydantic import ValidationError
import pytest
from src.platform.llm.factory import LLMFactory
from src.platform.llm.settings import LLMSettings

# Env var names defined by the prefix + field name
_ENV_VARS = (
    'LLM_MAX_LOADED_MODELS',
    'LLM_MAX_MESSAGES',
    'LLM_MAX_CHARS_PER_MESSAGE',
    'LLM_MAX_TOTAL_CHARS',
)


@pytest.fixture(autouse=True)
def _clear_llm_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no stray LLM_* env vars pollute defaults tests."""
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Settings-only tests
# ---------------------------------------------------------------------------


def test_defaults_when_env_unset() -> None:
    s = LLMSettings()
    assert s.max_loaded_models == 2
    assert s.max_messages == 32
    assert s.max_chars_per_message == 32_000
    assert s.max_total_chars == 128_000


def test_max_loaded_models_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LLM_MAX_LOADED_MODELS', '5')
    s = LLMSettings()
    assert s.max_loaded_models == 5


def test_max_messages_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LLM_MAX_MESSAGES', '64')
    s = LLMSettings()
    assert s.max_messages == 64


def test_max_chars_per_message_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LLM_MAX_CHARS_PER_MESSAGE', '4000')
    s = LLMSettings()
    assert s.max_chars_per_message == 4000


def test_max_total_chars_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LLM_MAX_TOTAL_CHARS', '16000')
    s = LLMSettings()
    assert s.max_total_chars == 16000


def test_rejects_zero_max_loaded_models(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LLM_MAX_LOADED_MODELS', '0')
    with pytest.raises(ValidationError):
        LLMSettings()


def test_rejects_negative_max_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LLM_MAX_MESSAGES', '-1')
    with pytest.raises(ValidationError):
        LLMSettings()


def test_rejects_non_integer_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LLM_MAX_TOTAL_CHARS', 'not-a-number')
    with pytest.raises(ValidationError):
        LLMSettings()


def test_extra_env_vars_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LLM_UNKNOWN_KNOB', '1')
    # Must not raise
    LLMSettings()


# ---------------------------------------------------------------------------
# Factory-wiring tests
# ---------------------------------------------------------------------------


def test_factory_uses_settings_when_max_loaded_models_omitted() -> None:
    s = LLMSettings(max_loaded_models=7)
    factory = LLMFactory(settings=s)
    assert factory._max_loaded_models == 7


def test_factory_explicit_max_loaded_models_overrides_settings() -> None:
    s = LLMSettings(max_loaded_models=7)
    factory = LLMFactory(max_loaded_models=3, settings=s)
    assert factory._max_loaded_models == 3


def test_factory_falls_back_to_env_when_neither_arg_given(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('LLM_MAX_LOADED_MODELS', '4')
    factory = LLMFactory()
    assert factory._max_loaded_models == 4

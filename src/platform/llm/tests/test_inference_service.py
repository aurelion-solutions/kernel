# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer tests for LLM inference orchestration.

All tests use the real async DB session from src/conftest.py via session_factory.
LLMFactory is always a fake to avoid real provider lifecycle.
FakeLLMProvider is defined here with configurable behaviour.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest
from src.platform.llm.exceptions import LLMInferenceValidationError, LLMProfileNotFoundError
from src.platform.llm.factory import LLMModelInactiveError
from src.platform.llm.inference_service import _validate_messages, run_inference, stream_inference
from src.platform.llm.models import LLMExecutionProfile, LLMModel, LLMProvider
from src.platform.llm.providers.base import LLMChunk, LLMMessage
from src.platform.llm.schemas import InferenceRequest, LLMMessageIn
from src.platform.logs.service import NoOpLogService
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

# ---------------------------------------------------------------------------
# FakeLLMProvider
# ---------------------------------------------------------------------------


class FakeLLMProvider:
    """Configurable fake provider for tests.

    Parameters
    ----------
    chunks:
        List of ``LLMChunk`` objects to yield (must end with a ``done=True`` chunk).
    raise_exc:
        If set, the provider raises this exception when streaming.
    abort_call_count:
        Tracks how many times ``abort()`` was called.
    """

    def __init__(
        self,
        chunks: list[LLMChunk] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._chunks = chunks or [
            LLMChunk(token='hello', done=False),
            LLMChunk(token=' world', done=False),
            LLMChunk(token='', done=True, output='hello world', tokens_used=2),
        ]
        self._raise_exc = raise_exc
        self.abort_call_count = 0

    async def stream(
        self,
        messages: list[LLMMessage],
        params: dict[str, Any],
    ) -> AsyncIterator[LLMChunk]:
        if self._raise_exc is not None:
            raise self._raise_exc
        for chunk in self._chunks:
            yield chunk

    async def abort(self) -> None:
        self.abort_call_count += 1


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(**overrides: Any) -> RuntimeSettingsConfig:
    """Create a RuntimeSettingsConfig with test-friendly defaults."""
    return RuntimeSettingsConfig(
        llm_max_loaded_models=2,
        llm_max_messages=overrides.get('max_messages', 32),
        llm_max_chars_per_message=overrides.get('max_chars_per_message', 32_000),
        llm_max_total_chars=overrides.get('max_total_chars', 128_000),
    )


async def _create_model_and_profile(session: Any, *, is_active: bool = True) -> tuple[LLMModel, LLMExecutionProfile]:
    """Insert an LLMModel + LLMExecutionProfile and flush."""
    model = LLMModel(
        name=f'test-model-{uuid.uuid4().hex[:8]}',
        provider=LLMProvider.llama_cpp,
        local_path='/fake/path.gguf',
        is_active=is_active,
        default_params={},
    )
    session.add(model)
    await session.flush()

    profile = LLMExecutionProfile(
        name=f'test-profile-{uuid.uuid4().hex[:8]}',
        model_id=model.id,
        param_overrides={},
    )
    session.add(profile)
    await session.flush()
    return model, profile


def _make_factory(provider: FakeLLMProvider) -> Any:
    factory = AsyncMock()
    factory.get = AsyncMock(return_value=provider)
    return factory


def _one_message() -> list[LLMMessageIn]:
    return [LLMMessageIn(role='user', content='hello')]


# ---------------------------------------------------------------------------
# Test 1 — happy path: tokens assembled, timing populated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_inference_happy_path(session_factory: Any) -> None:
    provider = FakeLLMProvider(
        chunks=[
            LLMChunk(token='hi', done=False),
            LLMChunk(token=' there', done=False),
            LLMChunk(token='!', done=False),
            LLMChunk(token='', done=True, output='hi there!', tokens_used=3),
        ]
    )
    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session)
        factory = _make_factory(provider)

        result = await run_inference(
            session,
            factory,
            request=InferenceRequest(execution_profile_id=profile.id, messages=_one_message()),
            settings=_make_settings(),
            log_service=NoOpLogService(),
        )

    assert result.output == 'hi there!'
    assert result.tokens_used == 3
    assert result.latency_ms >= 0
    assert result.ttft_ms is not None
    assert result.ttft_ms >= 0
    assert result.model_id == profile.model_id
    assert result.execution_profile_id == profile.id


# ---------------------------------------------------------------------------
# Test 2 — unknown profile raises LLMProfileNotFoundError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_inference_unknown_profile(session_factory: Any) -> None:
    factory = _make_factory(FakeLLMProvider())
    async with session_factory() as session:
        with pytest.raises(LLMProfileNotFoundError):
            await run_inference(
                session,
                factory,
                request=InferenceRequest(execution_profile_id=uuid.uuid4(), messages=_one_message()),
                settings=_make_settings(),
                log_service=NoOpLogService(),
            )


# ---------------------------------------------------------------------------
# Test 3 — inactive model raises LLMModelInactiveError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_inference_inactive_model(session_factory: Any) -> None:
    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session, is_active=True)

        factory = AsyncMock()
        factory.get = AsyncMock(side_effect=LLMModelInactiveError('model inactive'))

        with pytest.raises(LLMModelInactiveError):
            await run_inference(
                session,
                factory,
                request=InferenceRequest(execution_profile_id=profile.id, messages=_one_message()),
                settings=_make_settings(),
                log_service=NoOpLogService(),
            )


# ---------------------------------------------------------------------------
# Test 4 — too many messages
# ---------------------------------------------------------------------------


def test_validate_messages_too_many() -> None:
    settings = _make_settings(max_messages=2)
    messages = [LLMMessage(role='user', content='x') for _ in range(3)]
    with pytest.raises(LLMInferenceValidationError, match='Too many messages'):
        _validate_messages(messages, settings)


# ---------------------------------------------------------------------------
# Test 5 — single message too long
# ---------------------------------------------------------------------------


def test_validate_messages_message_too_long() -> None:
    settings = _make_settings(max_chars_per_message=10)
    messages = [LLMMessage(role='user', content='a' * 11)]
    with pytest.raises(LLMInferenceValidationError, match='Message content too long'):
        _validate_messages(messages, settings)


# ---------------------------------------------------------------------------
# Test 6 — total chars exceed limit
# ---------------------------------------------------------------------------


def test_validate_messages_total_too_long() -> None:
    settings = _make_settings(max_total_chars=20)
    messages = [
        LLMMessage(role='user', content='a' * 11),
        LLMMessage(role='assistant', content='b' * 11),
    ]
    with pytest.raises(LLMInferenceValidationError, match='Total message chars too long'):
        _validate_messages(messages, settings)


# ---------------------------------------------------------------------------
# Test 7 — success log emitted with correct fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_inference_emits_success_log(session_factory: Any) -> None:
    provider = FakeLLMProvider()

    log_service = MagicMock(spec=NoOpLogService)
    log_service.emit_safe = MagicMock()

    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session)
        factory = _make_factory(provider)

        await run_inference(
            session,
            factory,
            request=InferenceRequest(execution_profile_id=profile.id, messages=_one_message()),
            settings=_make_settings(),
            log_service=log_service,
            correlation_id='test-corr-id',
        )

    log_service.emit_safe.assert_called_once()
    call_kwargs = log_service.emit_safe.call_args.kwargs
    payload = call_kwargs.get('payload', {})
    assert payload.get('status') == 'success'
    assert 'tokens_used' in payload
    assert 'latency_ms' in payload
    assert call_kwargs.get('correlation_id') == 'test-corr-id'


# ---------------------------------------------------------------------------
# Test 8 — provider error → error log emitted, exc propagates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_inference_emits_error_log(session_factory: Any) -> None:
    exc = RuntimeError('provider exploded')
    provider = FakeLLMProvider(raise_exc=exc)

    log_service = MagicMock(spec=NoOpLogService)
    log_service.emit_safe = MagicMock()

    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session)
        factory = _make_factory(provider)

        with pytest.raises(RuntimeError):
            await run_inference(
                session,
                factory,
                request=InferenceRequest(execution_profile_id=profile.id, messages=_one_message()),
                settings=_make_settings(),
                log_service=log_service,
            )

    log_service.emit_safe.assert_called_once()
    call_kwargs = log_service.emit_safe.call_args.kwargs
    payload = call_kwargs.get('payload', {})
    assert payload.get('status') == 'error'
    assert payload.get('error_code') == 'RuntimeError'


# ---------------------------------------------------------------------------
# Test 9 — stream_inference yields token dicts then final
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_inference_yields_token_then_final(session_factory: Any) -> None:
    import json as _json

    chunks = [
        LLMChunk(token='foo', done=False),
        LLMChunk(token='bar', done=False),
        LLMChunk(token='', done=True, output='foobar', tokens_used=2),
    ]
    provider = FakeLLMProvider(chunks=chunks)

    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session)
        factory = _make_factory(provider)

        events: list[dict[str, Any]] = []
        async for event in stream_inference(
            session,
            factory,
            request=InferenceRequest(execution_profile_id=profile.id, messages=_one_message()),
            settings=_make_settings(),
            log_service=NoOpLogService(),
        ):
            events.append(event)

    # Two token events + one final event
    assert len(events) == 3
    first = _json.loads(events[0]['data'])
    assert first['done'] is False
    assert 'token' in first

    final = _json.loads(events[-1]['data'])
    assert final['done'] is True
    assert 'output' in final
    assert 'tokens_used' in final


# ---------------------------------------------------------------------------
# Test 10 — stream_inference aborts on disconnect
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_inference_aborts_on_disconnect(session_factory: Any) -> None:
    # Provider yields two tokens then final
    chunks = [
        LLMChunk(token='tok1', done=False),
        LLMChunk(token='tok2', done=False),
        LLMChunk(token='', done=True, output='tok1tok2', tokens_used=2),
    ]
    provider = FakeLLMProvider(chunks=chunks)

    call_count = 0

    async def is_disconnected() -> bool:
        nonlocal call_count
        call_count += 1
        # Disconnect after first token
        return call_count > 1

    log_service = MagicMock(spec=NoOpLogService)
    log_service.emit_safe = MagicMock()

    events: list[dict[str, Any]] = []
    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session)
        factory = _make_factory(provider)

        try:
            async for event in stream_inference(
                session,
                factory,
                request=InferenceRequest(execution_profile_id=profile.id, messages=_one_message()),
                settings=_make_settings(),
                log_service=log_service,
                is_disconnected=is_disconnected,
            ):
                events.append(event)
        except asyncio.CancelledError:
            pass

    # provider.abort() must have been called
    assert provider.abort_call_count >= 1

    # After CancelledError, stream_inference re-raises without yielding an extra event.
    # At least the first token event should have been received before disconnect.
    assert len(events) >= 1

    # Log must have been emitted with status=aborted
    log_service.emit_safe.assert_called()
    found_aborted = any(
        call.kwargs.get('payload', {}).get('status') == 'aborted' for call in log_service.emit_safe.call_args_list
    )
    assert found_aborted

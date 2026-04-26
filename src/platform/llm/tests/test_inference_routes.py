# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP-layer tests for LLM inference routes.

Uses the shared ``app`` / ``client`` fixtures from src/conftest.py.
``get_llm_factory`` is overridden per test so real providers are never loaded.
``FakeLLMProvider`` from the service test module is reused.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import json
from typing import Any
from unittest.mock import AsyncMock
import uuid

import pytest
from src.platform.llm.deps import get_llm_factory
from src.platform.llm.models import LLMExecutionProfile, LLMModel, LLMProvider
from src.platform.llm.providers.base import LLMChunk, LLMMessage

# ---------------------------------------------------------------------------
# FakeLLMProvider (local copy to keep tests independent)
# ---------------------------------------------------------------------------


class FakeLLMProvider:
    """Minimal fake provider for route-layer tests."""

    def __init__(
        self,
        chunks: list[LLMChunk] | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._chunks = chunks or [
            LLMChunk(token='hello', done=False),
            LLMChunk(token='', done=True, output='hello', tokens_used=1),
        ]
        self._raise_exc = raise_exc

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
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _create_model_and_profile(session: Any, *, is_active: bool = True) -> tuple[LLMModel, LLMExecutionProfile]:
    model = LLMModel(
        name=f'route-model-{uuid.uuid4().hex[:8]}',
        provider=LLMProvider.llama_cpp,
        local_path='/fake/path.gguf',
        is_active=is_active,
        default_params={},
    )
    session.add(model)
    await session.flush()

    profile = LLMExecutionProfile(
        name=f'route-profile-{uuid.uuid4().hex[:8]}',
        model_id=model.id,
        param_overrides={},
    )
    session.add(profile)
    await session.flush()
    return model, profile


def _make_fake_factory(provider: FakeLLMProvider) -> Any:
    factory = AsyncMock()
    factory.get = AsyncMock(return_value=provider)
    return factory


def _inference_body(profile_id: uuid.UUID, messages: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        'execution_profile_id': str(profile_id),
        'messages': messages or [{'role': 'user', 'content': 'hello'}],
    }


# ---------------------------------------------------------------------------
# Test 1 — POST /inference returns 200 with correct shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_inference_returns_200(app: Any, client: Any, session_factory: Any) -> None:
    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session)
        await session.commit()

    provider = FakeLLMProvider()
    app.dependency_overrides[get_llm_factory] = lambda: _make_fake_factory(provider)

    try:
        resp = await client.post('/api/v0/inference', json=_inference_body(profile.id))
        assert resp.status_code == 200
        data = resp.json()
        assert 'output' in data
        assert 'tokens_used' in data
        assert 'latency_ms' in data
        assert 'model_id' in data
        assert 'execution_profile_id' in data
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


# ---------------------------------------------------------------------------
# Test 2 — unknown profile → 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_inference_unknown_profile_returns_404(app: Any, client: Any) -> None:
    provider = FakeLLMProvider()
    app.dependency_overrides[get_llm_factory] = lambda: _make_fake_factory(provider)

    try:
        resp = await client.post('/api/v0/inference', json=_inference_body(uuid.uuid4()))
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


# ---------------------------------------------------------------------------
# Test 3 — inactive model → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_inference_inactive_model_returns_422(app: Any, client: Any, session_factory: Any) -> None:
    from src.platform.llm.factory import LLMModelInactiveError

    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session, is_active=True)
        await session.commit()

    factory = AsyncMock()
    factory.get = AsyncMock(side_effect=LLMModelInactiveError('inactive'))
    app.dependency_overrides[get_llm_factory] = lambda: factory

    try:
        resp = await client.post('/api/v0/inference', json=_inference_body(profile.id))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


# ---------------------------------------------------------------------------
# Test 4 — too many messages → 422
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_inference_too_many_messages_returns_422(app: Any, client: Any, session_factory: Any) -> None:
    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session)
        await session.commit()

    provider = FakeLLMProvider()
    app.dependency_overrides[get_llm_factory] = lambda: _make_fake_factory(provider)

    # Default max_messages = 32; send 33
    messages = [{'role': 'user', 'content': 'x'} for _ in range(33)]

    try:
        resp = await client.post('/api/v0/inference', json=_inference_body(profile.id, messages))
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


# ---------------------------------------------------------------------------
# Test 5 — invalid role → 422 (Pydantic)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_inference_invalid_role_returns_422(app: Any, client: Any) -> None:
    provider = FakeLLMProvider()
    app.dependency_overrides[get_llm_factory] = lambda: _make_fake_factory(provider)

    try:
        resp = await client.post(
            '/api/v0/inference',
            json={
                'execution_profile_id': str(uuid.uuid4()),
                'messages': [{'role': 'admin', 'content': 'hello'}],
            },
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


# ---------------------------------------------------------------------------
# Test 6 — extra field in body → 422 (extra='forbid')
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_inference_extra_field_returns_422(app: Any, client: Any) -> None:
    provider = FakeLLMProvider()
    app.dependency_overrides[get_llm_factory] = lambda: _make_fake_factory(provider)

    try:
        resp = await client.post(
            '/api/v0/inference',
            json={
                'execution_profile_id': str(uuid.uuid4()),
                'messages': [{'role': 'user', 'content': 'hi'}],
                'foo': 1,
            },
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


# ---------------------------------------------------------------------------
# Test 7 — SSE stream: token events + final done event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_inference_stream_yields_sse_events(app: Any, client: Any, session_factory: Any) -> None:
    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session)
        await session.commit()

    chunks = [
        LLMChunk(token='foo', done=False),
        LLMChunk(token='bar', done=False),
        LLMChunk(token='', done=True, output='foobar', tokens_used=2),
    ]
    provider = FakeLLMProvider(chunks=chunks)
    app.dependency_overrides[get_llm_factory] = lambda: _make_fake_factory(provider)

    try:
        resp = await client.post(
            '/api/v0/inference/stream',
            json=_inference_body(profile.id),
            headers={'Accept': 'text/event-stream'},
        )
        assert resp.status_code == 200

        # Parse SSE lines
        raw_lines = [line for line in resp.text.split('\n') if line.startswith('data:')]
        assert len(raw_lines) >= 2

        first_payload = json.loads(raw_lines[0].removeprefix('data:').strip())
        assert first_payload.get('done') is False
        assert 'token' in first_payload

        last_payload = json.loads(raw_lines[-1].removeprefix('data:').strip())
        assert last_payload.get('done') is True
        assert 'output' in last_payload
        assert 'tokens_used' in last_payload
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


# ---------------------------------------------------------------------------
# Test 8 — SSE stream: provider error → error event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_inference_stream_provider_error_emits_error_event(
    app: Any, client: Any, session_factory: Any
) -> None:
    async with session_factory() as session:
        _, profile = await _create_model_and_profile(session)
        await session.commit()

    provider = FakeLLMProvider(raise_exc=RuntimeError('boom'))
    app.dependency_overrides[get_llm_factory] = lambda: _make_fake_factory(provider)

    try:
        resp = await client.post(
            '/api/v0/inference/stream',
            json=_inference_body(profile.id),
            headers={'Accept': 'text/event-stream'},
        )
        # sse-starlette always returns 200 for the SSE response;
        # errors are signalled inside the stream payload.
        assert resp.status_code == 200
        raw_lines = [line for line in resp.text.split('\n') if line.startswith('data:')]
        assert raw_lines, 'expected at least one SSE data line'
        last_payload = json.loads(raw_lines[-1].removeprefix('data:').strip())
        assert last_payload.get('done') is True
        assert 'error' in last_payload
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)

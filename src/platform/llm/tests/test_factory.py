# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LLMFactory — in-process LRU registry.

All tests mock AsyncSession and LlamaCppProvider.
No real DB, no real llama_cpp import.
asyncio_mode='auto' via pyproject.toml.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from src.platform.llm.factory import (
    LLMFactory,
    LLMModelInactiveError,
    LLMModelNotFoundError,
    LLMProviderNotSupportedError,
)
from src.platform.llm.models import LLMProvider
from src.platform.llm.providers import AbstractLLMProvider

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def make_model(
    provider: LLMProvider = LLMProvider.llama_cpp,
    is_active: bool = True,
    local_path: str | None = '/fake/m.gguf',
) -> Any:
    """Build a fake LLMModel-like namespace (not persisted to DB)."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        name='test-model',
        provider=provider,
        local_path=local_path,
        is_active=is_active,
        default_params={},
    )


def make_session(model: Any) -> AsyncMock:
    """Return a fake AsyncSession whose .get() returns the given model."""
    session = AsyncMock()
    session.get = AsyncMock(return_value=model)
    return session


class FakeProvider(AbstractLLMProvider):
    """Minimal fake AbstractLLMProvider with tracked abort()."""

    def __init__(self, **_kwargs: Any) -> None:
        self._abort_mock: AsyncMock = AsyncMock()

    def stream(
        self,
        messages: Any,
        params: Any,
    ) -> AsyncIterator[Any]:
        async def _empty() -> AsyncIterator[Any]:
            return
            yield  # pragma: no cover

        return _empty()

    async def abort(self) -> None:
        await self._abort_mock()


def make_fake_provider_cls() -> tuple[type[FakeProvider], list[FakeProvider]]:
    """Return (patched class, list that accumulates created instances)."""
    created: list[FakeProvider] = []

    class _TrackedProvider(FakeProvider):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            created.append(self)

    return _TrackedProvider, created


def cache_of(factory: LLMFactory) -> dict[uuid.UUID, AbstractLLMProvider]:
    """Access the private _cache dict."""
    return factory._cache  # noqa: SLF001


def load_locks_of(factory: LLMFactory) -> dict[uuid.UUID, asyncio.Lock]:
    """Access the private _load_locks dict."""
    return factory._load_locks  # noqa: SLF001


def abort_count(p: FakeProvider) -> int:
    """Return how many times abort() was awaited on a FakeProvider."""
    return p._abort_mock.await_count


# ---------------------------------------------------------------------------
# 1. Loads and caches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_loads_and_caches_provider() -> None:
    model = make_model()
    session = make_session(model)
    ProviderCls, created = make_fake_provider_cls()

    with patch('src.platform.llm.factory.LlamaCppProvider', ProviderCls):
        factory = LLMFactory(max_loaded_models=2)
        p1 = await factory.get(session, model.id)
        p2 = await factory.get(session, model.id)

    assert p1 is p2
    assert len(created) == 1


# ---------------------------------------------------------------------------
# 2. Model missing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_raises_when_model_missing() -> None:
    session = make_session(None)
    factory = LLMFactory()

    with pytest.raises(LLMModelNotFoundError):
        await factory.get(session, uuid.uuid4())

    assert len(load_locks_of(factory)) == 0
    assert len(cache_of(factory)) == 0


# ---------------------------------------------------------------------------
# 3. Model inactive
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_raises_when_model_inactive() -> None:
    model = make_model(is_active=False)
    session = make_session(model)
    factory = LLMFactory()

    with pytest.raises(LLMModelInactiveError):
        await factory.get(session, model.id)


# ---------------------------------------------------------------------------
# 4. Unsupported provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_raises_for_unsupported_provider() -> None:
    model = make_model(provider=LLMProvider.openai)
    session = make_session(model)
    factory = LLMFactory()

    with pytest.raises(LLMProviderNotSupportedError):
        await factory.get(session, model.id)


# ---------------------------------------------------------------------------
# 5. Missing local_path for llama_cpp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_raises_when_local_path_missing_for_llama_cpp() -> None:
    model = make_model(local_path=None)
    session = make_session(model)
    factory = LLMFactory()

    with pytest.raises(LLMProviderNotSupportedError):
        await factory.get(session, model.id)


# ---------------------------------------------------------------------------
# 6. LRU eviction — A B C → A evicted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lru_evicts_least_recently_used() -> None:
    ProviderCls, created = make_fake_provider_cls()

    with patch('src.platform.llm.factory.LlamaCppProvider', ProviderCls):
        factory = LLMFactory(max_loaded_models=2)

        models = [make_model() for _ in range(3)]
        sessions = [make_session(m) for m in models]

        p_a = await factory.get(sessions[0], models[0].id)
        p_b = await factory.get(sessions[1], models[1].id)
        p_c = await factory.get(sessions[2], models[2].id)  # should evict A

    assert isinstance(p_a, FakeProvider)
    assert isinstance(p_b, FakeProvider)
    assert isinstance(p_c, FakeProvider)
    # A was evicted, abort called once
    assert abort_count(p_a) == 1
    # B and C are still in cache
    assert abort_count(p_b) == 0
    assert abort_count(p_c) == 0
    assert models[0].id not in cache_of(factory)
    assert models[1].id in cache_of(factory)
    assert models[2].id in cache_of(factory)


# ---------------------------------------------------------------------------
# 7. LRU marks most recently used on hit — A B, get A, load C → B evicted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lru_marks_most_recently_used_on_hit() -> None:
    ProviderCls, _ = make_fake_provider_cls()

    with patch('src.platform.llm.factory.LlamaCppProvider', ProviderCls):
        factory = LLMFactory(max_loaded_models=2)

        models = [make_model() for _ in range(3)]
        sessions = [make_session(m) for m in models]

        p_a = await factory.get(sessions[0], models[0].id)
        p_b = await factory.get(sessions[1], models[1].id)

        # re-access A → A becomes MRU
        await factory.get(sessions[0], models[0].id)

        # load C → B should be evicted (LRU)
        p_c = await factory.get(sessions[2], models[2].id)

    assert isinstance(p_a, FakeProvider)
    assert isinstance(p_b, FakeProvider)
    assert isinstance(p_c, FakeProvider)
    assert abort_count(p_b) == 1
    assert abort_count(p_a) == 0
    assert abort_count(p_c) == 0
    assert models[1].id not in cache_of(factory)
    assert models[0].id in cache_of(factory)
    assert models[2].id in cache_of(factory)


# ---------------------------------------------------------------------------
# 8. Concurrent get — constructor called exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_get_loads_once() -> None:
    block_event = asyncio.Event()
    call_count = 0

    class BlockingProvider(FakeProvider):
        def __init__(self, **kwargs: Any) -> None:
            nonlocal call_count
            call_count += 1
            super().__init__(**kwargs)

    model = make_model()
    session = make_session(model)

    original_construct = LLMFactory._construct  # noqa: SLF001

    async def slow_construct(self: LLMFactory, sess: Any, mid: Any) -> AbstractLLMProvider:
        await block_event.wait()
        result: AbstractLLMProvider = await original_construct(self, sess, mid)
        return result

    with patch('src.platform.llm.factory.LlamaCppProvider', BlockingProvider):
        factory = LLMFactory()

        with patch.object(LLMFactory, '_construct', slow_construct):
            task1 = asyncio.create_task(factory.get(session, model.id))
            task2 = asyncio.create_task(factory.get(session, model.id))
            await asyncio.sleep(0)  # let both tasks start
            block_event.set()
            p1, p2 = await asyncio.gather(task1, task2)

    assert p1 is p2
    assert call_count == 1


# ---------------------------------------------------------------------------
# 9. Concurrent get for different ids loads in parallel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_get_for_different_ids_loads_in_parallel() -> None:
    ProviderCls, created = make_fake_provider_cls()

    with patch('src.platform.llm.factory.LlamaCppProvider', ProviderCls):
        factory = LLMFactory(max_loaded_models=2)

        models = [make_model(), make_model()]
        sessions = [make_session(m) for m in models]

        p1, p2 = await asyncio.gather(
            factory.get(sessions[0], models[0].id),
            factory.get(sessions[1], models[1].id),
        )

    assert p1 is not p2
    assert len(created) == 2
    assert models[0].id in cache_of(factory)
    assert models[1].id in cache_of(factory)


# ---------------------------------------------------------------------------
# 10. invalidate drops and aborts; next get triggers fresh load
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_drops_and_aborts() -> None:
    ProviderCls, created = make_fake_provider_cls()

    with patch('src.platform.llm.factory.LlamaCppProvider', ProviderCls):
        factory = LLMFactory()

        model = make_model()
        session = make_session(model)

        p1 = await factory.get(session, model.id)
        await factory.invalidate(model.id)

        assert isinstance(p1, FakeProvider)
        assert abort_count(p1) == 1
        assert model.id not in cache_of(factory)

        p2 = await factory.get(session, model.id)

    assert p2 is not p1
    assert len(created) == 2


# ---------------------------------------------------------------------------
# 11. invalidate of unknown id is noop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_unknown_id_is_noop() -> None:
    factory = LLMFactory()
    # Must not raise
    await factory.invalidate(uuid.uuid4())


# ---------------------------------------------------------------------------
# 12. invalidate during in-flight load discards result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_during_inflight_load_discards_result() -> None:
    block = asyncio.Event()
    produced_providers: list[FakeProvider] = []

    class BlockingProvider(FakeProvider):
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            produced_providers.append(self)

    model = make_model()
    session = make_session(model)

    original_construct = LLMFactory._construct  # noqa: SLF001

    async def slow_construct(self: LLMFactory, sess: Any, mid: Any) -> AbstractLLMProvider:
        await block.wait()
        result: AbstractLLMProvider = await original_construct(self, sess, mid)
        return result

    with patch('src.platform.llm.factory.LlamaCppProvider', BlockingProvider):
        factory = LLMFactory()

        with patch.object(LLMFactory, '_construct', slow_construct):
            load_task = asyncio.create_task(factory.get(session, model.id))
            await asyncio.sleep(0)  # let load_task reach block.wait()

            # invalidate while loader is blocked
            await factory.invalidate(model.id)

            block.set()

            # The load will detect the stale lock and raise LLMModelNotFoundError
            with pytest.raises(LLMModelNotFoundError):
                await load_task

    # The produced provider should have been aborted
    assert len(produced_providers) == 1
    assert abort_count(produced_providers[0]) == 1

    # Cache must not contain the discarded provider
    assert model.id not in cache_of(factory)

    # Next get should trigger a fresh load
    with patch('src.platform.llm.factory.LlamaCppProvider', BlockingProvider):
        fresh = await factory.get(session, model.id)

    assert len(produced_providers) == 2
    assert isinstance(fresh, FakeProvider)
    assert fresh is produced_providers[1]


# ---------------------------------------------------------------------------
# 13. invalidate_all aborts every cached instance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalidate_all_aborts_every_cached_instance() -> None:
    ProviderCls, created = make_fake_provider_cls()

    with patch('src.platform.llm.factory.LlamaCppProvider', ProviderCls):
        factory = LLMFactory(max_loaded_models=3)

        models = [make_model() for _ in range(3)]
        for m in models:
            await factory.get(make_session(m), m.id)

        assert len(cache_of(factory)) == 3

        await factory.invalidate_all()

    assert len(cache_of(factory)) == 0
    for p in created:
        assert isinstance(p, FakeProvider)
        assert abort_count(p) == 1


# ---------------------------------------------------------------------------
# 14. Constructor rejects zero or negative capacity
# ---------------------------------------------------------------------------


def test_constructor_rejects_zero_or_negative_capacity() -> None:
    with pytest.raises(ValueError):
        LLMFactory(max_loaded_models=0)

    with pytest.raises(ValueError):
        LLMFactory(max_loaded_models=-5)

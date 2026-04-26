# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for LlamaCppProvider.

All tests run without a real llama_cpp install — the module is monkeypatched
via pytest fixtures. No GGUF files are downloaded.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import types
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Fake llama_cpp module used in all patched tests
# ---------------------------------------------------------------------------


def _make_fake_llama_module(llama_cls: Any) -> types.ModuleType:
    """Return a minimal fake llama_cpp module containing llama_cls as Llama."""
    mod = types.ModuleType('llama_cpp')
    mod.Llama = llama_cls  # type: ignore[attr-defined]
    return mod


class _FakeLlama:
    """Test double for llama_cpp.Llama.

    Captures constructor kwargs. create_chat_completion returns a generator of
    three OpenAI-shaped chunks: 'Hello', ' world', and an empty-delta sentinel.
    """

    def __init__(self, model_path: str, **kwargs: Any) -> None:
        self.model_path = model_path
        self.init_kwargs = kwargs
        self.last_completion_kwargs: dict[str, Any] = {}

    def create_chat_completion(
        self,
        messages: list[Any],
        stream: bool = False,
        **kwargs: Any,
    ) -> Any:
        self.last_completion_kwargs = kwargs

        def _gen() -> Any:
            yield {'choices': [{'delta': {'content': 'Hello'}}]}
            yield {'choices': [{'delta': {'content': ' world'}}]}
            yield {'choices': [{'delta': {}}]}  # empty delta — sentinel

        return _gen()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_gguf(tmp_path: Any) -> str:
    """Create a real (empty) file at tmp_path/model.gguf and return its path."""
    p = tmp_path / 'model.gguf'
    p.write_bytes(b'')
    return str(p)


@pytest.fixture()
def patched_llama_cpp(monkeypatch: pytest.MonkeyPatch) -> _FakeLlama:
    """Monkeypatch src.platform.llm.providers.llama_cpp with _FakeLlama.

    Returns the _FakeLlama CLASS (not instance) so each test can inspect
    constructor calls. The fixture also injects a fake module into sys.modules
    so that the conditional import succeeds.
    """
    import src.platform.llm.providers.llama_cpp as provider_mod

    fake_mod = _make_fake_llama_module(_FakeLlama)
    sys.modules['llama_cpp'] = fake_mod

    monkeypatch.setattr(provider_mod, '_llama_cpp_mod', fake_mod)
    monkeypatch.setattr(provider_mod, '_LLAMA_CPP_AVAILABLE', True)

    yield _FakeLlama  # type: ignore[misc]

    # Cleanup — remove the fake module so other tests stay unaffected
    sys.modules.pop('llama_cpp', None)


# ---------------------------------------------------------------------------
# Tests 1–8: construction and repr
# ---------------------------------------------------------------------------


def test_init_raises_when_package_unavailable(tmp_gguf: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Test 1 — LLMProviderUnavailableError when llama_cpp is not installed."""
    import src.platform.llm.providers.llama_cpp as provider_mod
    from src.platform.llm.providers.llama_cpp import LLMProviderUnavailableError

    monkeypatch.setattr(provider_mod, '_LLAMA_CPP_AVAILABLE', False)
    with pytest.raises(LLMProviderUnavailableError):
        provider_mod.LlamaCppProvider(local_path=tmp_gguf)


def test_init_raises_when_path_missing(patched_llama_cpp: Any) -> None:
    """Test 2 — LlamaCppLoadError for a non-existent path; message has no path."""
    from src.platform.llm.providers.llama_cpp import LlamaCppLoadError, LlamaCppProvider

    missing = '/nonexistent/path/model.gguf'
    with pytest.raises(LlamaCppLoadError) as exc_info:
        LlamaCppProvider(local_path=missing)

    assert missing not in str(exc_info.value)
    assert missing not in repr(exc_info.value)


def test_init_raises_when_path_is_directory(patched_llama_cpp: Any, tmp_path: Any) -> None:
    """Test 3 — LlamaCppLoadError when local_path points to a directory."""
    from src.platform.llm.providers.llama_cpp import LlamaCppLoadError, LlamaCppProvider

    with pytest.raises(LlamaCppLoadError):
        LlamaCppProvider(local_path=str(tmp_path))


@pytest.mark.skipif(os.name == 'nt', reason='Unix permission bits only')
def test_init_raises_when_path_unreadable(patched_llama_cpp: Any, tmp_path: Any) -> None:
    """Test 4 — LlamaCppLoadError when model file is not readable."""
    from src.platform.llm.providers.llama_cpp import LlamaCppLoadError, LlamaCppProvider

    p = tmp_path / 'noaccess.gguf'
    p.write_bytes(b'')
    p.chmod(0o000)
    try:
        with pytest.raises(LlamaCppLoadError):
            LlamaCppProvider(local_path=str(p))
    finally:
        p.chmod(0o644)


def test_init_succeeds_and_loads_model(patched_llama_cpp: Any, tmp_gguf: str) -> None:
    """Test 5 — successful construction creates a _llm attribute."""
    from src.platform.llm.providers.llama_cpp import LlamaCppProvider

    provider = LlamaCppProvider(local_path=tmp_gguf)
    assert isinstance(provider._llm, _FakeLlama)
    assert provider._llm.model_path == tmp_gguf


def test_init_extracts_only_load_params(patched_llama_cpp: Any, tmp_gguf: str) -> None:
    """Test 6 — only known load-time keys are forwarded to Llama.__init__."""
    from src.platform.llm.providers.llama_cpp import LlamaCppProvider

    provider = LlamaCppProvider(
        local_path=tmp_gguf,
        default_params={
            'n_ctx': 2048,
            'temperature': 0.7,  # generation-only — must NOT reach Llama.__init__
            'unknown_key': 'value',
        },
    )
    assert provider._llm.init_kwargs == {'n_ctx': 2048}


def test_init_wraps_llama_cpp_load_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_gguf: str,
) -> None:
    """Test 7 — exception from Llama() is wrapped in LlamaCppLoadError; no upstream text."""
    import src.platform.llm.providers.llama_cpp as provider_mod
    from src.platform.llm.providers.llama_cpp import LlamaCppLoadError

    class _BrokenLlama:
        def __init__(self, model_path: str, **kwargs: Any) -> None:
            raise RuntimeError('secret path /private/model.gguf could not be loaded')

    fake_mod = _make_fake_llama_module(_BrokenLlama)
    monkeypatch.setattr(provider_mod, '_llama_cpp_mod', fake_mod)
    monkeypatch.setattr(provider_mod, '_LLAMA_CPP_AVAILABLE', True)

    with pytest.raises(LlamaCppLoadError) as exc_info:
        provider_mod.LlamaCppProvider(local_path=tmp_gguf)

    error_str = str(exc_info.value)
    assert '/private/model.gguf' not in error_str
    assert tmp_gguf not in error_str
    assert 'secret' not in error_str


def test_repr_does_not_leak_path(patched_llama_cpp: Any, tmp_gguf: str) -> None:
    """Test 8 — __repr__ equals 'LlamaCppProvider(loaded=True)' and has no path."""
    from src.platform.llm.providers.llama_cpp import LlamaCppProvider

    provider = LlamaCppProvider(local_path=tmp_gguf)
    r = repr(provider)
    assert r == 'LlamaCppProvider(loaded=True)'
    assert tmp_gguf not in r


# ---------------------------------------------------------------------------
# Tests 9–14: async streaming and concurrency
# ---------------------------------------------------------------------------


async def test_stream_yields_progress_then_terminal_chunk(
    patched_llama_cpp: Any,
    tmp_gguf: str,
) -> None:
    """Test 9 — full stream: progress chunks then terminal with assembled output."""
    from src.platform.llm.providers.llama_cpp import LlamaCppProvider

    provider = LlamaCppProvider(local_path=tmp_gguf)
    chunks = []
    async for chunk in provider.stream([], {}):
        chunks.append(chunk)

    # Two content chunks + one terminal
    assert len(chunks) == 3
    assert chunks[0].token == 'Hello'
    assert chunks[0].done is False
    assert chunks[1].token == ' world'
    assert chunks[1].done is False
    terminal = chunks[2]
    assert terminal.done is True
    assert terminal.output == 'Hello world'
    assert terminal.tokens_used == 2


async def test_stream_passes_generation_params_only(
    patched_llama_cpp: Any,
    tmp_gguf: str,
) -> None:
    """Test 10 — generation params forwarded; load-time keys and unknowns dropped."""
    from src.platform.llm.providers.llama_cpp import LlamaCppProvider

    provider = LlamaCppProvider(
        local_path=tmp_gguf,
        default_params={'n_ctx': 512, 'temperature': 0.5},
    )
    # Call-time override
    async for _ in provider.stream([], {'temperature': 0.9, 'n_gpu_layers': 4}):
        pass

    # n_ctx and n_gpu_layers are load-time keys — must not appear
    kwargs = provider._llm.last_completion_kwargs
    assert 'n_ctx' not in kwargs
    assert 'n_gpu_layers' not in kwargs
    # temperature merged with call-time override winning
    assert kwargs.get('temperature') == 0.9


async def test_stream_aborts_between_tokens(
    patched_llama_cpp: Any,
    tmp_gguf: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 11 — abort() halts generation after first token; terminal chunk emitted."""
    import src.platform.llm.providers.llama_cpp as provider_mod

    gate = threading.Event()

    class _GatedLlama(_FakeLlama):
        def create_chat_completion(self, messages: Any, stream: bool = False, **kwargs: Any) -> Any:
            def _gen() -> Any:
                yield {'choices': [{'delta': {'content': 'Hello'}}]}
                gate.wait()  # Block until test calls gate.set()
                yield {'choices': [{'delta': {'content': ' world'}}]}

            return _gen()

    fake_mod = _make_fake_llama_module(_GatedLlama)
    monkeypatch.setattr(provider_mod, '_llama_cpp_mod', fake_mod)

    from src.platform.llm.providers.llama_cpp import LlamaCppProvider

    provider = LlamaCppProvider(local_path=tmp_gguf)
    chunks: list[Any] = []

    async def _consume() -> None:
        async for chunk in provider.stream([], {}):
            chunks.append(chunk)
            if chunk.token == 'Hello':
                # Abort and unblock the gate concurrently
                await provider.abort()
                gate.set()

    await _consume()

    # Must have at least one content chunk + exactly one terminal done=True
    assert any(c.done for c in chunks), 'terminal chunk missing'
    terminal = next(c for c in chunks if c.done)
    assert terminal.done is True
    assert terminal.tokens_used is not None


async def test_stream_wraps_iterator_failure(
    patched_llama_cpp: Any,
    tmp_gguf: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Test 12 — exception during iteration raises LlamaCppGenerationError; message scrubbed."""
    import src.platform.llm.providers.llama_cpp as provider_mod

    class _ExplodingLlama(_FakeLlama):
        def create_chat_completion(self, messages: Any, stream: bool = False, **kwargs: Any) -> Any:
            def _gen() -> Any:
                yield {'choices': [{'delta': {'content': 'Hi'}}]}
                raise RuntimeError('internal CUDA error at /secret/path/kernel.cu')
                yield  # noqa: unreachable

            return _gen()

    fake_mod = _make_fake_llama_module(_ExplodingLlama)
    monkeypatch.setattr(provider_mod, '_llama_cpp_mod', fake_mod)

    from src.platform.llm.providers.llama_cpp import LlamaCppGenerationError, LlamaCppProvider

    provider = LlamaCppProvider(local_path=tmp_gguf)

    with pytest.raises(LlamaCppGenerationError) as exc_info:
        async for _ in provider.stream([], {}):
            pass

    error_str = str(exc_info.value)
    assert '/secret/path' not in error_str
    assert 'CUDA' not in error_str


async def test_abort_is_idempotent(patched_llama_cpp: Any, tmp_gguf: str) -> None:
    """Test 13 — calling abort() multiple times must not raise."""
    from src.platform.llm.providers.llama_cpp import LlamaCppProvider

    provider = LlamaCppProvider(local_path=tmp_gguf)
    await provider.abort()
    await provider.abort()  # second call must be fine
    await provider.abort()


async def test_concurrent_streams_are_serialized(
    patched_llama_cpp: Any,
    tmp_gguf: str,
) -> None:
    """Test 14 — two concurrent stream() calls are serialized by _active_lock.

    Note: Step 6 (LLMFactory) promotes this per-instance lock to a per-model_id
    lock shared across instances.
    """
    from src.platform.llm.providers.llama_cpp import LlamaCppProvider

    provider = LlamaCppProvider(local_path=tmp_gguf)

    async def _collect() -> list[Any]:
        chunks = []
        async for c in provider.stream([], {}):
            chunks.append(c)
        return chunks

    results = await asyncio.gather(_collect(), _collect())
    # Both streams must complete without RuntimeError and each must have a terminal chunk
    for result in results:
        assert any(c.done for c in result), 'missing terminal chunk in one of the concurrent streams'

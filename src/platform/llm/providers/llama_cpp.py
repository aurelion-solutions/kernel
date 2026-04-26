# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""LlamaCppProvider — concrete AbstractLLMProvider implementation.

Loads a GGUF model via llama-cpp-python, exposes an async-generator stream(),
and an idempotent cooperative abort() checked between tokens.

No DB, no HTTP, no logging. Optional dependency — install with:
    pip install "aurelion-kernel[llm-llama-cpp]"
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import os
from typing import Any

from src.platform.llm.providers.base import AbstractLLMProvider, LLMChunk, LLMMessage

# ---------------------------------------------------------------------------
# Optional dependency: llama-cpp-python
# ---------------------------------------------------------------------------

try:
    import llama_cpp as _llama_cpp_mod

    _LLAMA_CPP_AVAILABLE: bool = True
except ImportError:
    _llama_cpp_mod = None
    _LLAMA_CPP_AVAILABLE = False


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class LlamaCppProviderError(Exception):
    """Base exception for all LlamaCppProvider errors."""


class LLMProviderUnavailableError(LlamaCppProviderError):
    """Raised when llama-cpp-python is not installed."""


class LlamaCppLoadError(LlamaCppProviderError):
    """Raised when the GGUF model cannot be loaded.

    __str__ and __repr__ deliberately omit the original exception message and
    any file-system paths so that tracebacks never leak sensitive information.
    """


class LlamaCppGenerationError(LlamaCppProviderError):
    """Raised when token generation fails mid-stream."""


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


class _StopSentinel(Exception):
    """Re-routes StopIteration out of coroutines (PEP 479).

    Python 3.7+ converts a bare StopIteration raised inside a coroutine into
    RuntimeError. _next_or_stop() translates it to _StopSentinel so that the
    async generator loop can catch it explicitly without triggering PEP 479.
    """


def _next_or_stop(it: Any) -> Any:
    """Pull the next item from a synchronous iterator.

    Raises _StopSentinel instead of StopIteration so the caller (running in a
    coroutine) does not trigger the PEP 479 RuntimeError conversion.
    """
    try:
        return next(it)
    except StopIteration:
        raise _StopSentinel from None


def _extract_delta_text(raw_chunk: Any) -> str:
    """Extract the delta content string from an OpenAI-shaped streaming chunk.

    Returns '' on any structural mismatch — a malformed chunk is silently
    skipped rather than crashing the generator.
    """
    try:
        return raw_chunk['choices'][0].get('delta', {}).get('content', '') or ''
    except (KeyError, IndexError, TypeError):
        return ''


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_LOAD_PARAM_KEYS: frozenset[str] = frozenset({'n_ctx', 'n_gpu_layers', 'n_threads', 'seed', 'verbose'})
_GEN_PARAM_KEYS: frozenset[str] = frozenset(
    {
        'temperature',
        'top_p',
        'top_k',
        'max_tokens',
        'stop',
        'repeat_penalty',
        'presence_penalty',
        'frequency_penalty',
    }
)


@dataclass(frozen=True, slots=True)
class LlamaCppConfig:
    """Immutable configuration snapshot for a LlamaCppProvider instance."""

    local_path: str
    default_params: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LlamaCppProvider(AbstractLLMProvider):
    """Concrete provider backed by llama-cpp-python (GGUF models).

    Sync->async streaming pattern (precedent for Step 6):
    - asyncio.to_thread() wraps the one-shot create_chat_completion() call
      which may perform non-trivial setup.
    - Per-chunk pull uses loop.run_in_executor(None, _next_or_stop, iterator)
      on the default ThreadPoolExecutor -- same pool as to_thread().
    - The event loop remains alive between chunks so abort_event is observed
      and other coroutines can proceed.
    """

    __slots__ = ('_config', '_llm', '_abort_event', '_active_lock')

    def __init__(
        self,
        *,
        local_path: str,
        default_params: dict[str, Any] | None = None,
    ) -> None:
        if not _LLAMA_CPP_AVAILABLE:
            raise LLMProviderUnavailableError('llama_cpp_python is not installed')

        if not local_path:
            raise LlamaCppLoadError('local_path is required')
        if not os.path.exists(local_path):
            raise LlamaCppLoadError('model file not found')
        if not os.path.isfile(local_path):
            raise LlamaCppLoadError('local_path is not a regular file')
        if not os.access(local_path, os.R_OK):
            raise LlamaCppLoadError('model file is not readable')

        self._config = LlamaCppConfig(
            local_path=local_path,
            default_params=dict(default_params or {}),
        )
        self._abort_event: asyncio.Event = asyncio.Event()
        self._active_lock: asyncio.Lock = asyncio.Lock()

        load_kwargs = self._extract_load_params(self._config.default_params)
        try:
            self._llm: Any = _llama_cpp_mod.Llama(model_path=local_path, **load_kwargs)
        except Exception as exc:
            raise LlamaCppLoadError('failed to load model') from exc

    @staticmethod
    def _extract_load_params(params: dict[str, Any]) -> dict[str, Any]:
        """Return only the keys accepted by Llama.__init__."""
        return {k: v for k, v in params.items() if k in _LOAD_PARAM_KEYS}

    @staticmethod
    def _extract_generation_params(params: dict[str, Any]) -> dict[str, Any]:
        """Return only the keys accepted by Llama.create_chat_completion()."""
        return {k: v for k, v in params.items() if k in _GEN_PARAM_KEYS}

    def stream(
        self,
        messages: list[LLMMessage],
        params: dict[str, Any],
    ) -> AsyncIterator[LLMChunk]:
        """Async generator yielding token chunks.

        Declared as a plain def to satisfy the ABC signature (see base.py
        comment on mypy async-generator overrides). Returns a true async
        generator object at runtime.
        """
        return self._stream_impl(messages, params)

    async def _stream_impl(
        self,
        messages: list[LLMMessage],
        params: dict[str, Any],
    ) -> AsyncIterator[LLMChunk]:
        async with self._active_lock:
            self._abort_event.clear()
            effective = {
                **self._extract_generation_params(self._config.default_params),
                **self._extract_generation_params(params),
            }
            llama_messages = [{'role': m.role, 'content': m.content} for m in messages]
            loop = asyncio.get_running_loop()
            output_buf: list[str] = []
            tokens_used = 0

            try:
                iterator: Any = await asyncio.to_thread(
                    self._llm.create_chat_completion,
                    messages=llama_messages,
                    stream=True,
                    **effective,
                )
                while True:
                    if self._abort_event.is_set():
                        yield LLMChunk(
                            token='',
                            done=True,
                            output=''.join(output_buf),
                            tokens_used=tokens_used,
                        )
                        return
                    try:
                        raw = await loop.run_in_executor(None, _next_or_stop, iterator)
                    except _StopSentinel:
                        break
                    text = _extract_delta_text(raw)
                    if not text:
                        continue
                    output_buf.append(text)
                    tokens_used += 1
                    yield LLMChunk(token=text, done=False)

                yield LLMChunk(
                    token='',
                    done=True,
                    output=''.join(output_buf),
                    tokens_used=tokens_used,
                )
            except (LlamaCppGenerationError, _StopSentinel):
                raise
            except Exception as exc:
                raise LlamaCppGenerationError('generation failed') from exc

    async def abort(self) -> None:
        """Signal the active stream to stop between tokens. Idempotent."""
        self._abort_event.set()

    def __repr__(self) -> str:
        return 'LlamaCppProvider(loaded=True)'

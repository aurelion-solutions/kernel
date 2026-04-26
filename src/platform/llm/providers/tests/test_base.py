# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Contract tests for LLMMessage, LLMChunk, and AbstractLLMProvider."""

from collections.abc import AsyncIterator
import dataclasses
from typing import Any

import pytest
from src.platform.llm.providers.base import (
    AbstractLLMProvider,
    LLMChunk,
    LLMMessage,
)


def test_llm_message_is_frozen() -> None:
    msg = LLMMessage(role='user', content='hi')
    with pytest.raises(dataclasses.FrozenInstanceError):
        msg.content = 'x'  # type: ignore[misc]


@pytest.mark.parametrize('role', ['system', 'user', 'assistant'])
def test_llm_message_accepts_all_three_roles(role: str) -> None:
    msg = LLMMessage(role=role, content='x')  # type: ignore[arg-type]
    assert msg.role == role


def test_llm_chunk_progress_defaults() -> None:
    chunk = LLMChunk(token='Hi', done=False)
    assert chunk.output is None
    assert chunk.tokens_used is None


def test_llm_chunk_terminal_carries_output_and_tokens() -> None:
    chunk = LLMChunk(token='', done=True, output='Hi there', tokens_used=42)
    assert chunk.token == ''
    assert chunk.done is True
    assert chunk.output == 'Hi there'
    assert chunk.tokens_used == 42


def test_llm_chunk_is_frozen() -> None:
    chunk = LLMChunk(token='x', done=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        chunk.token = 'y'  # type: ignore[misc]


def test_abstract_llm_provider_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        AbstractLLMProvider()  # type: ignore[abstract]


def test_concrete_subclass_must_implement_both_methods() -> None:
    class OnlyStream(AbstractLLMProvider):
        async def stream(
            self,
            messages: list[LLMMessage],
            params: dict[str, Any],
        ) -> AsyncIterator[LLMChunk]:
            yield LLMChunk(token='x', done=True, output='x', tokens_used=1)

    with pytest.raises(TypeError) as exc_info:
        OnlyStream()  # type: ignore[abstract]
    assert 'abort' in str(exc_info.value)

    class BothMethods(AbstractLLMProvider):
        async def stream(
            self,
            messages: list[LLMMessage],
            params: dict[str, Any],
        ) -> AsyncIterator[LLMChunk]:
            yield LLMChunk(token='x', done=True, output='x', tokens_used=1)

        async def abort(self) -> None:
            return None

    provider = BothMethods()
    assert isinstance(provider, AbstractLLMProvider)


async def test_concrete_stream_yields_chunks() -> None:
    class StubProvider(AbstractLLMProvider):
        async def stream(
            self,
            messages: list[LLMMessage],
            params: dict[str, Any],
        ) -> AsyncIterator[LLMChunk]:
            yield LLMChunk(token='Hello', done=False)
            yield LLMChunk(token='', done=True, output='Hello', tokens_used=5)

        async def abort(self) -> None:
            return None

    provider = StubProvider()
    chunks: list[LLMChunk] = []
    async for chunk in provider.stream([], {}):
        chunks.append(chunk)

    assert len(chunks) == 2
    assert chunks[1].done is True
    assert chunks[1].output is not None

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Abstract provider interface for the LLM platform.

Concrete providers live in sibling modules.
Type-only — no DB, no HTTP.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

# Allowed LLMMessage roles. Mirrors phase_14.md §Inference Contract validation constraints.
# The string set is the contract; do NOT widen without a phase update.
LLMRole = Literal['system', 'user', 'assistant']


@dataclass(frozen=True, slots=True)
class LLMMessage:
    """One chat-style message passed to a provider.

    Immutable. The provider adapts a list of these into its provider-specific
    format (Step 5+).
    """

    role: LLMRole
    content: str


@dataclass(frozen=True, slots=True)
class LLMChunk:
    """One streamed token chunk.

    While ``done=False`` only ``token`` is meaningful. The terminal chunk carries
    ``done=True`` and MUST populate ``output`` (full assembled text) and
    ``tokens_used``. Providers MUST emit exactly one terminal chunk per stream —
    including aborted streams (Step 5 contract).
    """

    token: str
    done: bool
    output: str | None = None
    tokens_used: int | None = None


class AbstractLLMProvider(ABC):
    """Provider contract.

    Implementations adapt ``list[LLMMessage]`` + a free-form ``params`` dict into
    a provider-native request, then yield ``LLMChunk``s.

    Implementations MUST NOT write to the database, MUST NOT log secrets,
    MUST NOT leak internal paths or model parameters in error messages
    (phase_14.md §Strict Rules).
    """

    @abstractmethod
    def stream(
        self,
        messages: list[LLMMessage],
        params: dict[str, Any],
    ) -> AsyncIterator[LLMChunk]:
        """Yield token chunks for the given messages.

        Implementations MUST be async generators (``async def`` + ``yield``).
        The return-type annotation is ``AsyncIterator[LLMChunk]`` — declared as
        a plain ``def`` in the ABC so that mypy accepts async-generator overrides
        without type conflicts (see mypy docs §Asynchronous Iterators). Callers
        consume the result with ``async for``. Implementations MUST emit a
        terminal ``LLMChunk(done=True, ...)`` even on abort.
        """
        raise NotImplementedError

    @abstractmethod
    async def abort(self) -> None:
        """Signal the running stream to stop.

        Idempotent — calling ``abort()`` when no stream is active MUST NOT raise.
        The active stream's running ``stream()`` generator MUST observe the signal
        between tokens and exit cleanly via the terminal
        ``LLMChunk(done=True, ...)``.
        """
        raise NotImplementedError

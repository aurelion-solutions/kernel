# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""LLMFactory — in-process LRU registry of AbstractLLMProvider instances.

Semantics
---------
* Keyed by ``LLMModel.id`` (UUID).
* Bounded LRU: at most ``max_loaded_models`` providers reside in memory at once.
  The least-recently-used entry is evicted and its ``abort()`` called when the
  cache is full.
* Two-lock model prevents deadlock and coalesces concurrent loads:
  - ``_cache_lock`` (asyncio.Lock) — short critical sections around the
    OrderedDict; never held across an ``await`` on external code.
  - ``_load_locks[model_id]`` (asyncio.Lock) — one per model; gates slow DB +
    provider-construction work.  Lock ordering is always
    ``acquire(load_lock) → acquire(_cache_lock) → release(_cache_lock) →
    release(load_lock)``, preventing cycles.
* ``abort()`` is always called OUTSIDE ``_cache_lock`` (collect-then-await
  pattern).  ``abort()`` is idempotent per ``AbstractLLMProvider`` contract.
* Race-safe invalidation: the loader captures a reference to its ``load_lock``
  before the slow path.  After construction, under ``_cache_lock``, it checks
  ``_load_locks.get(model_id) is load_lock``.  If ``invalidate()`` ran during
  the load, the identity check fails, the new provider is discarded (abort()
  called), and NOT inserted into the cache.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.llm.models import LLMModel, LLMProvider
from src.platform.llm.providers import AbstractLLMProvider, LlamaCppProvider
from src.platform.runtime_settings.schemas import RuntimeSettingsConfig

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class LLMFactoryError(Exception):
    """Base exception for LLMFactory errors."""


class LLMModelNotFoundError(LLMFactoryError):
    """No LLMModel row with the requested id exists."""


class LLMModelInactiveError(LLMFactoryError):
    """The requested LLMModel exists but is_active=False."""


class LLMProviderNotSupportedError(LLMFactoryError):
    """The provider type or configuration is not supported in this step."""


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class LLMFactory:
    """In-process LRU registry of AbstractLLMProvider instances.

    Parameters
    ----------
    max_loaded_models:
        Maximum number of providers held in memory at once.  Must be >= 1.
    """

    __slots__ = ('_max_loaded_models', '_cache', '_cache_lock', '_load_locks')

    def __init__(
        self,
        *,
        max_loaded_models: int | None = None,
        settings: RuntimeSettingsConfig | None = None,
    ) -> None:
        if max_loaded_models is None:
            if settings is not None:
                max_loaded_models = settings.llm_max_loaded_models
            else:
                max_loaded_models = 2
        if max_loaded_models < 1:
            raise ValueError('max_loaded_models must be >= 1')
        self._max_loaded_models: int = max_loaded_models
        self._cache: OrderedDict[uuid.UUID, AbstractLLMProvider] = OrderedDict()
        self._cache_lock: asyncio.Lock = asyncio.Lock()
        self._load_locks: dict[uuid.UUID, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(
        self,
        session: AsyncSession,
        model_id: uuid.UUID,
    ) -> AbstractLLMProvider:
        """Return a live provider for *model_id*, loading it if necessary.

        Cache hit: moves the entry to MRU position and returns immediately.
        Cache miss: acquires the per-model load lock, re-checks the cache
        (another coroutine may have loaded while we waited), then calls
        ``_construct`` outside both locks.

        ``session`` is used read-only; caller owns the transaction boundary.
        """
        # --- fast path: already cached ---
        async with self._cache_lock:
            if model_id in self._cache:
                self._cache.move_to_end(model_id)
                return self._cache[model_id]
            load_lock = self._load_locks.setdefault(model_id, asyncio.Lock())

        # --- slow path: wait for any concurrent load of the same model ---
        async with load_lock:
            # Re-check: someone may have populated the cache while we waited.
            async with self._cache_lock:
                if model_id in self._cache:
                    self._cache.move_to_end(model_id)
                    return self._cache[model_id]

            # _construct runs outside both locks.
            stale: AbstractLLMProvider | None = None
            evicted: list[AbstractLLMProvider] = []

            try:
                provider = await self._construct(session, model_id)
            except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
                # Clean up the per-model lock so the next caller can retry.
                async with self._cache_lock:
                    self._load_locks.pop(model_id, None)
                raise

            async with self._cache_lock:
                if self._load_locks.get(model_id) is not load_lock:
                    # invalidate() ran during our load — discard the result.
                    stale = provider
                else:
                    evicted = self._evict_lru_locked()
                    self._cache[model_id] = provider
                    self._load_locks.pop(model_id, None)

        # abort() is idempotent per AbstractLLMProvider contract.
        for p in evicted:
            try:
                await p.abort()
            except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
                pass
        if stale is not None:
            try:
                await stale.abort()
            except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
                pass

        if stale is not None:
            # Provide a usable return value by re-fetching from the cache or
            # performing a fresh load; for now raise to signal the caller that
            # the model was invalidated while loading.
            raise LLMModelNotFoundError('model was invalidated during load; please retry')

        return provider

    async def invalidate(self, model_id: uuid.UUID) -> None:
        """Remove *model_id* from the cache and abort its provider.

        Idempotent — no-op if the model is not cached.
        Removes the per-model load lock so any in-flight loader's identity
        check will detect the invalidation.
        """
        async with self._cache_lock:
            removed = self._cache.pop(model_id, None)
            self._load_locks.pop(model_id, None)
        if removed is not None:
            try:
                await removed.abort()
            except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
                pass

    async def invalidate_all(self) -> None:
        """Remove all cached providers and abort each one."""
        async with self._cache_lock:
            removed = list(self._cache.values())
            self._cache.clear()
            self._load_locks.clear()
        for p in removed:
            try:
                await p.abort()
            except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
                pass

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _construct(
        self,
        session: AsyncSession,
        model_id: uuid.UUID,
    ) -> AbstractLLMProvider:
        """Look up the DB row and instantiate the appropriate provider.

        Raises
        ------
        LLMModelNotFoundError
            No row for *model_id*.
        LLMModelInactiveError
            Row exists but ``is_active=False``.
        LLMProviderNotSupportedError
            Provider type is not ``llama_cpp``, or ``local_path`` is missing.
        """
        row: LLMModel | None = await session.get(LLMModel, model_id)
        if row is None:
            raise LLMModelNotFoundError(f'LLMModel not found: {model_id}')
        if not row.is_active:
            raise LLMModelInactiveError(f'LLMModel is inactive: {model_id}')

        if row.provider != LLMProvider.llama_cpp:
            raise LLMProviderNotSupportedError(f'provider {row.provider!r} is not supported in this step')
        if not row.local_path:
            raise LLMProviderNotSupportedError('llama_cpp provider requires local_path')

        default_params: dict[str, Any] = dict(row.default_params or {})
        return LlamaCppProvider(
            local_path=row.local_path,
            default_params=default_params,
        )

    def _evict_lru_locked(self) -> list[AbstractLLMProvider]:
        """Remove the least-recently-used entry if the cache is at capacity.

        Must be called under ``_cache_lock``.  Returns a list of evicted
        provider instances — the caller MUST call ``await p.abort()`` on each
        OUTSIDE the lock.
        """
        evicted: list[AbstractLLMProvider] = []
        while len(self._cache) >= self._max_loaded_models:
            # OrderedDict.popitem(last=False) removes the LRU entry.
            _, provider = self._cache.popitem(last=False)
            evicted.append(provider)
        return evicted

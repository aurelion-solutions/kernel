# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine-action registry for the native pipeline orchestrator.

Public surface
--------------
- :class:`ActionContext`  — frozen dataclass carried to every action handler.
- :class:`RegisteredAction`  — frozen dataclass stored in the registry.
- :class:`ActionRegistry`  — in-memory singleton: register / get / dispatch / all.
- :func:`register_action`  — decorator that registers async handlers.
- ``ACTION_REGISTRY``  — the singleton instance.

Custom exceptions (all inherit :class:`ActionRegistryError`)
------------------------------------------------------------
- :class:`DuplicateActionError`
- :class:`ActionNotFoundError`
- :class:`ActionArgsValidationError`
- :class:`ActionResultValidationError`

Design invariants
-----------------
- Registry starts **empty** on import.  Importing this module registers nothing.
- Registration is import-time and single-threaded; no locking.
- Only async handlers are accepted (``inspect.iscoroutinefunction``).
- Dispatch is the **sole** validation point for handler args — action handlers
  must not re-validate (ARCH_CONTEXT "Engine-action contract", line 356).
- Handlers must not call ``session.commit()`` or open their own sessions
  (ARCH_CONTEXT "Runner owns the transaction", line 357).
- No logging in this module — ``ActionContext.log_service`` is a passenger
  field for handlers and the future runner (Step 12) to use.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import inspect
from typing import Any
import uuid

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.logs.service import LogService

# ---------------------------------------------------------------------------
# Type alias for action handlers
# ---------------------------------------------------------------------------

ActionHandler = Callable[[BaseModel, 'ActionContext'], Awaitable[Any]]


# ---------------------------------------------------------------------------
# ActionContext — passed to every action handler by the runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionContext:
    """Immutable execution context injected into every action handler.

    The runner owns the transaction; the action MUST NOT call
    ``session.commit()`` or open its own session.  Exceptions propagate to
    the runner, which rolls back.
    """

    session: AsyncSession
    log_service: LogService
    pipeline_run_id: uuid.UUID
    step_run_id: uuid.UUID
    attempt: int
    worker_id: str | None


# ---------------------------------------------------------------------------
# RegisteredAction — registry entry
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegisteredAction:
    """Immutable record for a single registered engine action."""

    engine: str
    action: str
    handler: ActionHandler
    args_schema: type[BaseModel]
    result_schema: type[BaseModel]
    idempotent: bool


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class ActionRegistryError(Exception):
    """Base exception for all registry errors."""


class DuplicateActionError(ActionRegistryError):
    """Raised when the same (engine, action) pair is registered twice."""


class ActionNotFoundError(ActionRegistryError):
    """Raised when (engine, action) is not found in the registry."""


class ActionArgsValidationError(ActionRegistryError):
    """Raised when raw args fail ``args_schema`` validation."""

    def __init__(self, cause: ValidationError) -> None:
        super().__init__(str(cause))
        self.cause = cause


class ActionResultValidationError(ActionRegistryError):
    """Raised when the handler return value fails ``result_schema`` validation."""

    def __init__(self, cause: ValidationError) -> None:
        super().__init__(str(cause))
        self.cause = cause


# ---------------------------------------------------------------------------
# ActionRegistry
# ---------------------------------------------------------------------------


class ActionRegistry:
    """In-memory registry of engine actions.

    Keyed by ``(engine, action)``.  All methods are safe to call from async
    contexts; dispatch is the only awaitable operation.
    """

    def __init__(self) -> None:
        self._registry: dict[tuple[str, str], RegisteredAction] = {}

    def register(
        self,
        engine: str,
        action: str,
        args_schema: type[BaseModel],
        result_schema: type[BaseModel],
        idempotent: bool,
        handler: ActionHandler,
    ) -> None:
        """Register an action handler.

        Raises
        ------
        ValueError
            If ``engine`` or ``action`` is an empty string.
        TypeError
            If ``args_schema`` or ``result_schema`` is not a ``BaseModel`` subclass,
            or if ``handler`` is not an async function.
        DuplicateActionError
            If the ``(engine, action)`` pair is already registered.
        """
        if not engine:
            raise ValueError('engine must be a non-empty string')
        if not action:
            raise ValueError('action must be a non-empty string')

        if not (isinstance(args_schema, type) and issubclass(args_schema, BaseModel)):
            raise TypeError(f'args_schema must be a BaseModel subclass, got {args_schema!r}')
        if not (isinstance(result_schema, type) and issubclass(result_schema, BaseModel)):
            raise TypeError(f'result_schema must be a BaseModel subclass, got {result_schema!r}')
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(f'handler must be an async function, got {handler!r}')

        key = (engine, action)
        if key in self._registry:
            raise DuplicateActionError(f'Action ({engine!r}, {action!r}) is already registered')

        self._registry[key] = RegisteredAction(
            engine=engine,
            action=action,
            handler=handler,
            args_schema=args_schema,
            result_schema=result_schema,
            idempotent=idempotent,
        )

    def get(self, engine: str, action: str) -> RegisteredAction:
        """Return the registered action or raise :class:`ActionNotFoundError`."""
        key = (engine, action)
        try:
            return self._registry[key]
        except KeyError:
            raise ActionNotFoundError(f'Action ({engine!r}, {action!r}) not found in registry') from None

    async def dispatch(
        self,
        engine: str,
        action: str,
        raw_args: dict[str, Any],
        ctx: ActionContext,
    ) -> dict[str, Any]:
        """Validate args, invoke handler, validate result, return JSON-serialisable dict.

        Raises
        ------
        ActionNotFoundError
            If ``(engine, action)`` is not registered.
        ActionArgsValidationError
            If ``raw_args`` fails ``args_schema`` validation.
        ActionResultValidationError
            If handler return value fails ``result_schema`` validation.
        """
        entry = self.get(engine, action)

        try:
            args_model = entry.args_schema.model_validate(raw_args)
        except ValidationError as exc:
            raise ActionArgsValidationError(exc) from exc

        raw_result = await entry.handler(args_model, ctx)

        try:
            result_model = entry.result_schema.model_validate(raw_result)
        except ValidationError as exc:
            raise ActionResultValidationError(exc) from exc

        return result_model.model_dump(mode='json')

    def all(self) -> list[RegisteredAction]:
        """Return all registered actions (order is insertion order)."""
        return list(self._registry.values())

    def _clear_for_tests(self) -> None:
        """Remove all registrations.  Test-only; leading underscore signals the contract."""
        self._registry.clear()


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

ACTION_REGISTRY = ActionRegistry()


# ---------------------------------------------------------------------------
# @register_action decorator
# ---------------------------------------------------------------------------


def register_action(
    engine: str,
    action: str,
    args_schema: type[BaseModel],
    result_schema: type[BaseModel],
    idempotent: bool = True,
) -> Callable[[ActionHandler], ActionHandler]:
    """Decorator that registers an async action handler with :data:`ACTION_REGISTRY`.

    Returns the original function unchanged so it remains independently callable.

    Parameters
    ----------
    engine:
        Non-empty identifier for the owning engine (e.g. ``"provisioning"``).
    action:
        Non-empty identifier for the specific action (e.g. ``"assign_entitlement"``).
    args_schema:
        Pydantic ``BaseModel`` subclass describing the handler's input.
    result_schema:
        Pydantic ``BaseModel`` subclass describing the handler's output.
    idempotent:
        Metadata flag.  ``True`` (default) is the only value used in Phase 18.
        Registry stores it faithfully; enforcement is a future runner concern.
    """

    def decorator(fn: ActionHandler) -> ActionHandler:
        ACTION_REGISTRY.register(
            engine=engine,
            action=action,
            args_schema=args_schema,
            result_schema=result_schema,
            idempotent=idempotent,
            handler=fn,
        )
        return fn

    return decorator

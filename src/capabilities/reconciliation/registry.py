# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Module-level handler registry mapping artifact_type → Handler."""

from __future__ import annotations

import re

from src.capabilities.reconciliation.contracts import Handler, HandlerAlreadyRegisteredError

_ARTIFACT_TYPE_RE = re.compile(r'^[a-z][a-z0-9_]*$')

_HANDLERS: dict[str, Handler] = {}


def register_handler(artifact_type: str, handler: Handler) -> None:
    """Register a handler for the given artifact_type.

    Raises:
        ValueError: when ``artifact_type`` does not match ``^[a-z][a-z0-9_]*$``.
        HandlerAlreadyRegisteredError: when the type is already registered.
    """
    if not _ARTIFACT_TYPE_RE.match(artifact_type):
        raise ValueError(f'Invalid artifact_type {artifact_type!r}: must match ^[a-z][a-z0-9_]*$')
    if artifact_type in _HANDLERS:
        raise HandlerAlreadyRegisteredError(f'Handler already registered for artifact_type {artifact_type!r}')
    _HANDLERS[artifact_type] = handler


def get_handler(artifact_type: str) -> Handler | None:
    """Return the registered Handler, or None if no handler is registered."""
    return _HANDLERS.get(artifact_type)


def list_registered_types() -> list[str]:
    """Return sorted list of registered artifact_type strings (diagnostic use)."""
    return sorted(_HANDLERS)


def _reset_registry_for_tests() -> None:
    """Clear the registry.  Use only from test fixtures — NOT production code."""
    _HANDLERS.clear()

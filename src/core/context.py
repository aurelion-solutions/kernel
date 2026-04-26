# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Request-scoped correlation id.

Contract:
- ``correlation_id_var`` is set by :class:`~src.core.middleware.correlation.CorrelationIdMiddleware`
  at the start of every HTTP request and reset at the end.
- ``current_correlation_id()`` is a convenience accessor for services and builders.
- :func:`~src.platform.logs.schemas.new_root_log_event` and
  :func:`~src.platform.events.schemas.new_event_envelope` read this var as a fallback when the
  caller does not supply a ``correlation_id`` explicitly.
"""

from contextvars import ContextVar

correlation_id_var: ContextVar[str | None] = ContextVar('aurelion_correlation_id', default=None)


def current_correlation_id() -> str | None:
    """Return the correlation id for the current request context, or ``None`` outside a request."""
    return correlation_id_var.get()

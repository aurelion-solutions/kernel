# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EmailSender protocol — single contract for every email provider.

Providers must implement ``send(message)`` and return an ``EmailSendResult``.
The engine wrapper (``engines/notifications/``) calls this protocol; it never
imports a provider directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class EmailMessage:
    """A fully-rendered outgoing email message.

    Subject and body must already be template-rendered by the caller — the
    sender provider does not run Jinja2 itself (templates live in the engine
    layer, not in providers).
    """

    to: tuple[str, ...]
    subject: str
    body: str
    """Plain-text body. HTML support is out of scope for Phase 20."""
    locale: str = 'en'
    """ISO 639-1 locale hint used by providers that branch on language."""
    correlation_id: str | None = None
    """Optional correlation id, propagated to provider logs / headers."""


@dataclass(frozen=True)
class EmailSendResult:
    """Result of an ``EmailSender.send`` call.

    ``provider_message_id`` is the upstream id if the provider returned one
    (SMTP queue id, vendor message id, etc.). ``None`` for the ``file``
    provider.
    """

    sent: bool
    provider: str
    provider_message_id: str | None
    reason: str | None = None
    """Populated only when ``sent=False``. Free-form string for the operator."""


@runtime_checkable
class EmailSender(Protocol):
    """Minimal contract for outbound email delivery."""

    name: str
    """Provider name as registered in the factory (e.g. ``file``, ``smtp``)."""

    async def send(self, message: EmailMessage) -> EmailSendResult:
        """Deliver one email message.

        Implementations must:
        - return a result, not raise, for *delivery* failures (HTTP 4xx /
          SMTP NDR / vendor-side error) — the caller decides whether that
          fails the pipeline step.
        - raise for *transient infrastructure* failures (DNS down, socket
          timeout, unhandled exception). The engine wrapper turns those into
          retries via the orchestrator retry policy.
        """
        ...

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""InAppSender protocol — single contract for every in-app provider."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

RecipientKind = Literal['employee', 'nhi', 'operator']


@dataclass(frozen=True)
class InAppMessage:
    """A fully-rendered in-app notification.

    ``routing_key`` is the MQ routing key the kernel side emits on; the
    product MQ subscriber binds to a pattern containing it (e.g.
    ``notifications.inapp_journey.*``). ``case_id`` is product-side
    correlation; kernel does not interpret it but forwards it untouched
    so Journey can join the inbox row to a ``JourneyCase``.
    """

    template: str
    recipient_kind: RecipientKind
    recipient_id: str
    routing_key: str
    subject: str
    body: str
    link_to: str | None = None
    case_id: str | None = None
    ctx: Mapping[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None


@dataclass(frozen=True)
class InAppSendResult:
    sent: bool
    provider: str
    notification_id: str
    """UUID-string assigned by the kernel side; carried verbatim in the
    emitted event payload so the consumer can join on it."""
    reason: str | None = None


@runtime_checkable
class InAppSender(Protocol):
    name: str

    async def send(self, message: InAppMessage) -> InAppSendResult: ...

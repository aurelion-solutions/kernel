# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""WebhookSender protocol — single contract for every webhook provider.

A webhook target is fully described by ``url`` + ``payload``. Headers are
provider-decided (e.g. the ``http`` provider adds ``content-type:
application/json``; future signed providers would add an HMAC signature).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class WebhookMessage:
    url: str
    payload: Mapping[str, Any]
    """JSON-serializable payload."""
    headers: Mapping[str, str] = field(default_factory=dict)
    """Provider may merge these with its own defaults."""
    correlation_id: str | None = None


@dataclass(frozen=True)
class WebhookSendResult:
    sent: bool
    provider: str
    status_code: int | None
    reason: str | None = None


@runtime_checkable
class WebhookSender(Protocol):
    name: str

    async def send(self, message: WebhookMessage) -> WebhookSendResult: ...

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SmsSender protocol — single contract for every SMS provider."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SmsMessage:
    """A fully-rendered outgoing SMS message.

    ``to`` is an E.164-format phone number. The body must already be
    template-rendered. Length validation is provider-specific — Phase 20
    does not enforce a max length at the protocol level.
    """

    to: str
    body: str
    locale: str = 'en'
    correlation_id: str | None = None


@dataclass(frozen=True)
class SmsSendResult:
    sent: bool
    provider: str
    provider_message_id: str | None
    reason: str | None = None


@runtime_checkable
class SmsSender(Protocol):
    name: str

    async def send(self, message: SmsMessage) -> SmsSendResult: ...

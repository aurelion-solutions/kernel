# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Twilio SMS provider — minimal production-style implementation.

Twilio is configured via kernel secret store under
``notifications/sms/twilio/``:

- ``notifications/sms/twilio/account_sid`` (required)
- ``notifications/sms/twilio/auth_token``  (required)
- ``notifications/sms/twilio/from_number`` (required, E.164)

The provider talks to Twilio's REST API via plain ``httpx`` — we do not
pull in the ``twilio`` Python SDK to keep the dependency surface small.
Configuration missing → ``sent=False`` with a descriptive reason rather
than an exception. The engine wrapper surfaces that as a pipeline-step
failure that the operator can retry after fixing the secret.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx
from src.platform.notifications.sms.interface import SmsMessage, SmsSendResult
from src.platform.secrets.factory import secret_manager_factory


@dataclass(frozen=True)
class _TwilioConfig:
    account_sid: str
    auth_token: str
    from_number: str


def _read_secret(provider: Any, key: str) -> str:
    try:
        return str(provider.get_secret(key))
    except Exception:  # noqa: BLE001
        return ''


def _load_config() -> _TwilioConfig | tuple[None, str]:
    provider = secret_manager_factory.get('file')
    account_sid = _read_secret(provider, 'notifications/sms/twilio/account_sid')
    if account_sid == '':
        return None, 'missing secret: notifications/sms/twilio/account_sid'
    auth_token = _read_secret(provider, 'notifications/sms/twilio/auth_token')
    if auth_token == '':
        return None, 'missing secret: notifications/sms/twilio/auth_token'
    from_number = _read_secret(provider, 'notifications/sms/twilio/from_number')
    if from_number == '':
        return None, 'missing secret: notifications/sms/twilio/from_number'
    return _TwilioConfig(account_sid=account_sid, auth_token=auth_token, from_number=from_number)


class TwilioSmsSender:
    name = 'twilio'

    async def send(self, message: SmsMessage) -> SmsSendResult:
        cfg_or_reason = _load_config()
        if isinstance(cfg_or_reason, tuple):
            _, reason = cfg_or_reason
            return SmsSendResult(sent=False, provider=self.name, provider_message_id=None, reason=reason)
        cfg = cfg_or_reason

        url = f'https://api.twilio.com/2010-04-01/Accounts/{cfg.account_sid}/Messages.json'
        creds = base64.b64encode(f'{cfg.account_sid}:{cfg.auth_token}'.encode()).decode()
        headers = {
            'Authorization': f'Basic {creds}',
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        data = {'From': cfg.from_number, 'To': message.to, 'Body': message.body}

        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(url, headers=headers, data=data)

        if response.status_code >= 400:
            body = response.text
            return SmsSendResult(
                sent=False,
                provider=self.name,
                provider_message_id=None,
                reason=f'twilio {response.status_code}: {body[:200]}',
            )

        sid = response.json().get('sid')
        return SmsSendResult(sent=True, provider=self.name, provider_message_id=sid)

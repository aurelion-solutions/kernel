# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SMTP email provider — minimal production-style implementation.

The provider is configured via kernel secret store under ``notifications/email/smtp/``:

- ``notifications/email/smtp/host``        (required)
- ``notifications/email/smtp/port``        (default 587)
- ``notifications/email/smtp/username``    (required)
- ``notifications/email/smtp/password``    (required)
- ``notifications/email/smtp/from_address`` (required)
- ``notifications/email/smtp/use_starttls`` (default ``true``)

Secret loading happens lazily on the first ``send()`` call so the kernel can
boot without SMTP credentials present. The first invocation that lacks a
required secret returns ``sent=False`` with a descriptive reason, rather
than raising — the engine wrapper turns that into a pipeline-step failure
that an operator can re-trigger after fixing the secret.

The provider is intentionally minimal — no DKIM, no HTML, no attachments.
Phase 20 ships the wiring; richer envelope support lands when there is a
concrete tenant requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage as MimeEmailMessage
import smtplib
from typing import Any

from src.platform.notifications.email.interface import EmailMessage, EmailSendResult
from src.platform.secrets.factory import secret_manager_factory


@dataclass(frozen=True)
class _SmtpConfig:
    host: str
    port: int
    username: str
    password: str
    from_address: str
    use_starttls: bool


def _read_secret(provider: Any, key: str) -> str:
    """Return secret value or ``''`` if missing (provider-dependent)."""
    try:
        return str(provider.get_secret(key))
    except Exception:  # noqa: BLE001 — explicitly tolerate any provider-side failure
        return ''


def _load_config() -> _SmtpConfig | tuple[None, str]:
    """Resolve SMTP config from kernel secrets.

    Returns either a ``_SmtpConfig`` or ``(None, reason)`` on missing
    required secret. We never raise — pipeline steps should surface a
    descriptive ``EmailSendResult`` instead.
    """
    provider = secret_manager_factory.get('file')  # secret-store backend selection is global
    host = _read_secret(provider, 'notifications/email/smtp/host')
    if host == '':
        return None, 'missing secret: notifications/email/smtp/host'
    username = _read_secret(provider, 'notifications/email/smtp/username')
    if username == '':
        return None, 'missing secret: notifications/email/smtp/username'
    password = _read_secret(provider, 'notifications/email/smtp/password')
    if password == '':
        return None, 'missing secret: notifications/email/smtp/password'
    from_address = _read_secret(provider, 'notifications/email/smtp/from_address')
    if from_address == '':
        return None, 'missing secret: notifications/email/smtp/from_address'

    port_raw = _read_secret(provider, 'notifications/email/smtp/port')
    port = int(port_raw) if port_raw != '' else 587
    use_starttls_raw = _read_secret(provider, 'notifications/email/smtp/use_starttls')
    use_starttls = use_starttls_raw.lower() != 'false'

    return _SmtpConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        from_address=from_address,
        use_starttls=use_starttls,
    )


class SmtpEmailSender:
    """SMTP-backed email sender.

    Uses ``smtplib.SMTP`` synchronously; called from an async pipeline step
    the call blocks the event loop for the duration of the SMTP transaction.
    For the modest volume Phase 20 expects, this is acceptable. A future
    perf step can swap in ``aiosmtplib`` without changing the Protocol.
    """

    name = 'smtp'

    async def send(self, message: EmailMessage) -> EmailSendResult:
        cfg_or_reason = _load_config()
        if isinstance(cfg_or_reason, tuple):
            _, reason = cfg_or_reason
            return EmailSendResult(
                sent=False,
                provider=self.name,
                provider_message_id=None,
                reason=reason,
            )
        cfg = cfg_or_reason

        mime = MimeEmailMessage()
        mime['From'] = cfg.from_address
        mime['To'] = ', '.join(message.to)
        mime['Subject'] = message.subject
        if message.correlation_id is not None:
            mime['X-Correlation-Id'] = message.correlation_id
        mime.set_content(message.body)

        with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as smtp:
            if cfg.use_starttls:
                smtp.starttls()
            smtp.login(cfg.username, cfg.password)
            smtp.send_message(mime)

        return EmailSendResult(
            sent=True,
            provider=self.name,
            provider_message_id=None,
        )

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Engine actions for the notifications slice (Phase 20 K-I).

Four pipeline-callable actions wrap the
``platform/notifications/<channel>/`` subsystems:

- ``notifications.send_email``    (idempotent=False)
- ``notifications.send_sms``      (idempotent=False)
- ``notifications.send_webhook``  (idempotent=False)
- ``notifications.send_inapp``    (idempotent=False)

All four:

1. Render the requested template via
   :mod:`src.engines.notifications.template_engine`.
2. Build the channel-specific ``Message`` dataclass.
3. Delegate to the channel's factory-resolved provider.
4. Reshape the provider's ``SendResult`` into a Pydantic action result.

If the template is missing, or the provider returns ``sent=False``, the
action returns ``sent=False`` plus a ``reason`` string — the calling
pipeline step decides whether to fail (default ``on_error: fail``) or
continue.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from src.engines.notifications.template_engine import (
    RenderedTemplate,
    TemplateNotFoundError,
    render,
)
from src.platform.notifications.email.factory import email_sender_factory
from src.platform.notifications.email.interface import EmailMessage
from src.platform.notifications.inapp.factory import inapp_sender_factory
from src.platform.notifications.inapp.interface import InAppMessage
from src.platform.notifications.sms.factory import sms_sender_factory
from src.platform.notifications.sms.interface import SmsMessage
from src.platform.notifications.webhook.factory import webhook_sender_factory
from src.platform.notifications.webhook.interface import WebhookMessage
from src.platform.orchestrator.registry import ActionContext, register_action

# ---------------------------------------------------------------------------
# Shared result envelope
# ---------------------------------------------------------------------------


class _SendResultBase(BaseModel):
    model_config = ConfigDict(extra='forbid')

    sent: bool
    provider: str
    reason: str | None = None


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


class SendEmailArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')

    template: str = Field(..., min_length=1)
    to: tuple[str, ...]
    ctx: Mapping[str, Any] = Field(default_factory=dict)
    locale: str = 'en'
    correlation_id: str | None = None


class SendEmailResult(_SendResultBase):
    provider_message_id: str | None = None


def _render_or_failure(channel: str, template: str, ctx: Mapping[str, Any]) -> RenderedTemplate | str:
    """Return the rendered template, or a failure ``reason`` string."""
    try:
        return render(channel, template, ctx)
    except TemplateNotFoundError as exc:
        return f'template_not_found: {exc}'
    except Exception as exc:  # noqa: BLE001 — surface any Jinja2 error verbatim
        return f'template_render_error: {type(exc).__name__}: {exc}'


@register_action(  # type: ignore[arg-type]
    engine='notifications',
    action='send_email',
    args_schema=SendEmailArgs,
    result_schema=SendEmailResult,
    idempotent=False,
)
async def send_email_action(args: SendEmailArgs, _ctx: ActionContext) -> SendEmailResult:
    rendered = _render_or_failure('email', args.template, args.ctx)
    if isinstance(rendered, str):
        return SendEmailResult(sent=False, provider='unrendered', reason=rendered)

    sender = email_sender_factory.default()
    result = await sender.send(
        EmailMessage(
            to=tuple(args.to),
            subject=rendered.subject,
            body=rendered.body,
            locale=args.locale,
            correlation_id=args.correlation_id,
        )
    )
    return SendEmailResult(
        sent=result.sent,
        provider=result.provider,
        reason=result.reason,
        provider_message_id=result.provider_message_id,
    )


# ---------------------------------------------------------------------------
# SMS
# ---------------------------------------------------------------------------


class SendSmsArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')

    template: str = Field(..., min_length=1)
    to: str = Field(..., min_length=1)
    ctx: Mapping[str, Any] = Field(default_factory=dict)
    locale: str = 'en'
    correlation_id: str | None = None


class SendSmsResult(_SendResultBase):
    provider_message_id: str | None = None


@register_action(  # type: ignore[arg-type]
    engine='notifications',
    action='send_sms',
    args_schema=SendSmsArgs,
    result_schema=SendSmsResult,
    idempotent=False,
)
async def send_sms_action(args: SendSmsArgs, _ctx: ActionContext) -> SendSmsResult:
    rendered = _render_or_failure('sms', args.template, args.ctx)
    if isinstance(rendered, str):
        return SendSmsResult(sent=False, provider='unrendered', reason=rendered)

    # SMS templates only have a body block — subject is ignored.
    sender = sms_sender_factory.default()
    result = await sender.send(
        SmsMessage(
            to=args.to,
            body=rendered.body or rendered.subject,  # tolerate templates that only set subject
            locale=args.locale,
            correlation_id=args.correlation_id,
        )
    )
    return SendSmsResult(
        sent=result.sent,
        provider=result.provider,
        reason=result.reason,
        provider_message_id=result.provider_message_id,
    )


# ---------------------------------------------------------------------------
# Webhook
# ---------------------------------------------------------------------------


class SendWebhookArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')

    url: str = Field(..., min_length=1)
    template: str = Field(..., min_length=1)
    ctx: Mapping[str, Any] = Field(default_factory=dict)
    headers: Mapping[str, str] = Field(default_factory=dict)
    correlation_id: str | None = None


class SendWebhookResult(_SendResultBase):
    status_code: int | None = None


@register_action(  # type: ignore[arg-type]
    engine='notifications',
    action='send_webhook',
    args_schema=SendWebhookArgs,
    result_schema=SendWebhookResult,
    idempotent=False,
)
async def send_webhook_action(args: SendWebhookArgs, _ctx: ActionContext) -> SendWebhookResult:
    # Webhook templates render to a JSON-ish body string; we still go via the
    # template engine for consistency. The body is wrapped into payload as a
    # single 'body' field plus the verbatim ctx — this is the conservative
    # default; tenants can override by writing a template that returns a JSON
    # blob and decoding it on the receiving end.
    rendered = _render_or_failure('webhook', args.template, args.ctx)
    if isinstance(rendered, str):
        return SendWebhookResult(sent=False, provider='unrendered', reason=rendered)

    payload: dict[str, Any] = dict(args.ctx)
    payload['_template'] = args.template
    if rendered.subject != '':
        payload['_subject'] = rendered.subject
    if rendered.body != '':
        payload['_body'] = rendered.body

    sender = webhook_sender_factory.default()
    result = await sender.send(
        WebhookMessage(
            url=args.url,
            payload=payload,
            headers=args.headers,
            correlation_id=args.correlation_id,
        )
    )
    return SendWebhookResult(
        sent=result.sent,
        provider=result.provider,
        reason=result.reason,
        status_code=result.status_code,
    )


# ---------------------------------------------------------------------------
# In-app
# ---------------------------------------------------------------------------


class SendInappArgs(BaseModel):
    model_config = ConfigDict(extra='forbid')

    template: str = Field(..., min_length=1)
    recipient_kind: Literal['employee', 'nhi', 'operator']
    recipient_id: str = Field(..., min_length=1)
    routing_key: str = Field(..., min_length=1)
    ctx: Mapping[str, Any] = Field(default_factory=dict)
    link_to: str | None = None
    case_id: str | None = None
    correlation_id: str | None = None


class SendInappResult(_SendResultBase):
    notification_id: str | None = None


@register_action(  # type: ignore[arg-type]
    engine='notifications',
    action='send_inapp',
    args_schema=SendInappArgs,
    result_schema=SendInappResult,
    idempotent=False,
)
async def send_inapp_action(args: SendInappArgs, _ctx: ActionContext) -> SendInappResult:
    rendered = _render_or_failure('inapp', args.template, args.ctx)
    if isinstance(rendered, str):
        return SendInappResult(sent=False, provider='unrendered', reason=rendered)

    sender = inapp_sender_factory.default()
    result = await sender.send(
        InAppMessage(
            template=args.template,
            recipient_kind=args.recipient_kind,
            recipient_id=args.recipient_id,
            routing_key=args.routing_key,
            subject=rendered.subject,
            body=rendered.body,
            link_to=args.link_to,
            case_id=args.case_id,
            ctx=args.ctx,
            correlation_id=args.correlation_id,
        )
    )
    return SendInappResult(
        sent=result.sent,
        provider=result.provider,
        reason=result.reason,
        notification_id=result.notification_id,
    )

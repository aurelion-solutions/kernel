# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration-ish tests for the four notifications.* actions."""

from __future__ import annotations

import json
from pathlib import Path
import uuid

import pytest
from src.engines.notifications import actions  # noqa: F401 — ensures @register_action runs
from src.engines.notifications.actions import (
    SendEmailArgs,
    SendInappArgs,
    SendSmsArgs,
    SendWebhookArgs,
    send_email_action,
    send_inapp_action,
    send_sms_action,
    send_webhook_action,
)
from src.platform.logs.service import NoOpLogService
from src.platform.orchestrator.registry import (
    ACTION_REGISTRY,
    ActionContext,
)


def _ctx() -> ActionContext:
    return ActionContext(
        session=None,  # type: ignore[arg-type] — actions under test never touch session
        log_service=NoOpLogService(),
        pipeline_run_id=uuid.uuid4(),
        step_run_id=uuid.uuid4(),
        attempt=1,
        worker_id='test-worker',
    )


def _set_file_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every channel's file provider at the tmp dir."""
    monkeypatch.setenv('AURELION_NOTIFICATIONS_EMAIL_FILE_PATH', str(tmp_path / 'email.jsonl'))
    monkeypatch.setenv('AURELION_NOTIFICATIONS_SMS_FILE_PATH', str(tmp_path / 'sms.jsonl'))
    monkeypatch.setenv('AURELION_NOTIFICATIONS_WEBHOOK_FILE_PATH', str(tmp_path / 'webhook.jsonl'))
    monkeypatch.setenv('AURELION_NOTIFICATIONS_INAPP_FILE_PATH', str(tmp_path / 'inapp.jsonl'))
    # Force file providers so the tests never touch real SMTP/HTTP/MQ.
    monkeypatch.setenv('AURELION_NOTIFICATIONS_EMAIL_PROVIDER', 'file')
    monkeypatch.setenv('AURELION_NOTIFICATIONS_SMS_PROVIDER', 'file')
    monkeypatch.setenv('AURELION_NOTIFICATIONS_WEBHOOK_PROVIDER', 'file')
    monkeypatch.setenv('AURELION_NOTIFICATIONS_INAPP_PROVIDER', 'file')
    return tmp_path


def test_all_four_actions_registered() -> None:
    """Every notifications.send_* action ends up in the global ACTION_REGISTRY."""
    expected = {'send_email', 'send_sms', 'send_webhook', 'send_inapp'}
    registered = {entry.action for entry in ACTION_REGISTRY.all() if entry.engine == 'notifications'}
    assert expected.issubset(registered), f'missing actions: {expected - registered}'

    for action in expected:
        entry = ACTION_REGISTRY.get('notifications', action)
        assert entry.idempotent is False, f'{action} must be non-idempotent'


@pytest.mark.asyncio
async def test_send_email_uses_file_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _set_file_paths(tmp_path, monkeypatch)

    result = await send_email_action(
        SendEmailArgs(
            template='welcome_employee',
            to=('ada@example.com',),
            ctx={'first_name': 'Ada'},
            correlation_id='corr-x',
        ),
        _ctx(),
    )
    assert result.sent
    assert result.provider == 'file'

    record = json.loads((target / 'email.jsonl').read_text(encoding='utf-8').strip())
    assert record['channel'] == 'email'
    assert record['to'] == ['ada@example.com']
    assert 'Welcome' in record['subject']
    assert 'Hi Ada' in record['body']


@pytest.mark.asyncio
async def test_send_email_missing_template_returns_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_file_paths(tmp_path, monkeypatch)

    result = await send_email_action(
        SendEmailArgs(template='nope_nope_nope', to=('a@example.com',)),
        _ctx(),
    )
    assert result.sent is False
    assert result.provider == 'unrendered'
    assert result.reason is not None and 'template_not_found' in result.reason


@pytest.mark.asyncio
async def test_send_sms_uses_body_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _set_file_paths(tmp_path, monkeypatch)

    result = await send_sms_action(
        SendSmsArgs(template='leave_starts', to='+15555550100', ctx={'case_id': 'case-x'}),
        _ctx(),
    )
    assert result.sent
    record = json.loads((target / 'sms.jsonl').read_text(encoding='utf-8').strip())
    assert record['to'] == '+15555550100'
    assert 'case-x' in record['body']


@pytest.mark.asyncio
async def test_send_webhook_wraps_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _set_file_paths(tmp_path, monkeypatch)

    result = await send_webhook_action(
        SendWebhookArgs(
            url='https://example.com/h',
            template='case_completed',
            ctx={
                'case_id': 'case-y',
                'subject_ref': 'emp-1',
                'from_state': None,
                'to_state': 'active',
            },
        ),
        _ctx(),
    )
    assert result.sent
    record = json.loads((target / 'webhook.jsonl').read_text(encoding='utf-8').strip())
    assert record['url'] == 'https://example.com/h'
    assert record['payload']['case_id'] == 'case-y'
    assert '_body' in record['payload']
    body_decoded = json.loads(record['payload']['_body'])
    assert body_decoded['event'] == 'journey.case.completed'
    assert body_decoded['case_id'] == 'case-y'


@pytest.mark.asyncio
async def test_send_inapp_writes_routing_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = _set_file_paths(tmp_path, monkeypatch)

    result = await send_inapp_action(
        SendInappArgs(
            template='leaver_confirm_required',
            recipient_kind='operator',
            recipient_id='operator',
            routing_key='notifications.inapp_journey.dispatched',
            ctx={'case_id': 'case-z', 'destructive_count': 7},
            link_to='/cases/case-z',
            case_id='case-z',
        ),
        _ctx(),
    )
    assert result.sent
    assert result.notification_id is not None

    record = json.loads((target / 'inapp.jsonl').read_text(encoding='utf-8').strip())
    assert record['routing_key'] == 'notifications.inapp_journey.dispatched'
    assert record['recipient_kind'] == 'operator'
    assert record['case_id'] == 'case-z'
    assert '7 items' in record['subject']

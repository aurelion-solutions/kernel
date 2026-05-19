# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for inapp channel — file + eventbus providers + factory."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from src.platform.events.service import EventService
from src.platform.events.testing import CapturingEventService
from src.platform.notifications.inapp.factory import (
    InAppSenderFactory,
    UnsupportedInAppProviderError,
)
from src.platform.notifications.inapp.interface import InAppMessage
from src.platform.notifications.inapp.providers.eventbus import EventBusInAppSender
from src.platform.notifications.inapp.providers.file import FileInAppSender


@pytest.mark.asyncio
async def test_file_inapp_sender_writes_jsonl(tmp_path: Path) -> None:
    path = tmp_path / 'inapp.jsonl'
    sender = FileInAppSender(path=path)

    result = await sender.send(
        InAppMessage(
            template='leaver_confirm_required',
            recipient_kind='operator',
            recipient_id='operator',
            routing_key='notifications.inapp_journey.dispatched',
            subject='Confirm leaver apply',
            body='Please confirm the destructive apply for case X.',
            link_to='/cases/abc',
            case_id='case-abc',
            ctx={'destructive_count': 3, 'plan_id': 'plan-1'},
            correlation_id='c-1',
        )
    )
    assert result.sent and result.provider == 'file'
    assert len(result.notification_id) == 36  # uuid4 with hyphens

    record = json.loads(path.read_text(encoding='utf-8').strip())
    assert record['channel'] == 'inapp'
    assert record['notification_id'] == result.notification_id
    assert record['template'] == 'leaver_confirm_required'
    assert record['recipient_kind'] == 'operator'
    assert record['recipient_id'] == 'operator'
    assert record['routing_key'] == 'notifications.inapp_journey.dispatched'
    assert record['case_id'] == 'case-abc'
    assert record['ctx'] == {'destructive_count': 3, 'plan_id': 'plan-1'}


@pytest.mark.asyncio
async def test_eventbus_inapp_emits_event_on_provided_routing_key() -> None:
    capturing = CapturingEventService()
    events = EventService(sink=capturing)
    sender = EventBusInAppSender(event_service=events)

    result = await sender.send(
        InAppMessage(
            template='welcome_employee',
            recipient_kind='employee',
            recipient_id='emp-uuid-1',
            routing_key='notifications.inapp_journey.dispatched',
            subject='Welcome',
            body='Hi there',
            case_id='case-1',
            ctx={'first_day': '2026-06-01'},
        )
    )
    assert result.sent and result.provider == 'eventbus'

    emitted = capturing.filter_by_type('notifications.inapp_journey.dispatched')
    assert len(emitted) == 1
    payload = emitted[0].payload
    assert payload['notification_id'] == result.notification_id
    assert payload['recipient_kind'] == 'employee'
    assert payload['recipient_id'] == 'emp-uuid-1'
    assert payload['template'] == 'welcome_employee'
    assert payload['case_id'] == 'case-1'
    assert payload['link_to'] is None


def test_factory_default_is_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('AURELION_NOTIFICATIONS_INAPP_PROVIDER', raising=False)
    factory = InAppSenderFactory()
    assert factory.default().name == 'file'


def test_factory_eventbus_registered() -> None:
    factory = InAppSenderFactory()
    assert 'eventbus' in factory.list_names()
    assert factory.get('eventbus').name == 'eventbus'


def test_factory_unknown_provider_raises() -> None:
    factory = InAppSenderFactory()
    with pytest.raises(UnsupportedInAppProviderError):
        factory.get('no-such-provider')

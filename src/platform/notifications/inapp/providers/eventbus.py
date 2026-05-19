# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Event-bus in-app provider.

Each ``send`` call emits an MQ event on ``message.routing_key`` carrying
the fully-rendered notification. Product-side MQ subscribers (e.g.
Aurelion Journey's ``src/realtime/`` subscriber) bind to a routing-key
pattern they own (``notifications.inapp_journey.*``) and persist a row
in their own product DB.

The provider takes an ``EventService`` at construction so the kernel side
can wire whichever ``EventSink`` the runtime configures (noop in tests,
RabbitMQ in production).
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService, noop_event_service
from src.platform.notifications.inapp.interface import InAppMessage, InAppSendResult

_COMPONENT = 'notifications.inapp'


class EventBusInAppSender:
    name = 'eventbus'

    def __init__(self, event_service: EventService | None = None) -> None:
        self._events = event_service if event_service is not None else noop_event_service

    async def send(self, message: InAppMessage) -> InAppSendResult:
        notification_id = str(uuid.uuid4())
        correlation_id = message.correlation_id if message.correlation_id is not None else uuid.uuid4().hex

        envelope = EventEnvelope(
            event_id=uuid.uuid4(),
            event_type=message.routing_key,
            occurred_at=datetime.now(UTC),
            correlation_id=correlation_id,
            causation_id=None,
            payload={
                'notification_id': notification_id,
                'template': message.template,
                'recipient_kind': message.recipient_kind,
                'recipient_id': message.recipient_id,
                'subject': message.subject,
                'body': message.body,
                'link_to': message.link_to,
                'case_id': message.case_id,
                'ctx': dict(message.ctx),
                'created_at': datetime.now(UTC).isoformat(),
            },
            actor_kind=EventParticipantKind.COMPONENT,
            actor_id=_COMPONENT,
            target_kind=EventParticipantKind.SYSTEM,
            target_id=message.recipient_id,
        )
        await self._events.emit(envelope)

        return InAppSendResult(sent=True, provider=self.name, notification_id=notification_id)

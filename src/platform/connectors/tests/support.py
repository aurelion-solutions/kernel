# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Test helpers: connector ``is_online`` is derived from ``last_seen_at`` (see ``ConnectorInstance.is_online``)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.connectors.client import ConnectorClient
from src.platform.connectors.models import ConnectorInstance
from src.platform.connectors.repository import upsert_connector_instance

_STALE_LAST_SEEN = timedelta(minutes=10)


async def mark_connector_instance_offline(session: AsyncSession, instance_id: str) -> None:
    """Set ``last_seen_at`` so the instance is considered offline by ``ConnectorInstance.is_online``."""
    stale = datetime.now(UTC) - _STALE_LAST_SEEN
    await session.execute(
        update(ConnectorInstance).where(ConnectorInstance.instance_id == instance_id).values(last_seen_at=stale)
    )


async def seed_online_connector_instance(
    session_factory,
    *,
    instance_id: str = 'mock-connector',
    tags: list[str] | None = None,
) -> None:
    """Insert or refresh a connector instance so orchestration can resolve it for an application."""
    async with session_factory() as session:
        await upsert_connector_instance(
            session,
            instance_id=instance_id,
            tags=tags if tags is not None else [],
        )
        await session.commit()


class HandlerStubRPCClient:
    """RPC stub that delegates to an async handler ``(instance_id, operation, payload, result_storage_requested)``."""

    def __init__(
        self,
        handler: Callable[..., Awaitable[dict[str, Any]]],
    ) -> None:
        self._handler = handler

    async def request(
        self,
        *,
        instance_id: str,
        operation: str,
        payload: dict[str, Any],
        result_storage_requested: bool = False,
        correlation_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        return await self._handler(
            instance_id,
            operation,
            payload,
            result_storage_requested,
        )

    def close(self) -> None:
        return None


class RecordingStubRPCClient:
    """RPC client stub: returns canned responses per ``operation``; records calls."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        *,
        instance_id: str,
        operation: str,
        payload: dict[str, Any],
        result_storage_requested: bool = False,
        correlation_id: str | None = None,
        trace_parent_event_id: str | None = None,
        trace_initiator_type: str | None = None,
        trace_initiator_id: str | None = None,
        trace_target_type: str | None = None,
        trace_target_id: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                'instance_id': instance_id,
                'operation': operation,
                'payload': payload,
                'result_storage_requested': result_storage_requested,
                'correlation_id': correlation_id,
                'trace_parent_event_id': trace_parent_event_id,
                'trace_initiator_type': trace_initiator_type,
                'trace_initiator_id': trace_initiator_id,
                'trace_target_type': trace_target_type,
                'trace_target_id': trace_target_id,
            },
        )
        return self.responses[operation]

    def close(self) -> None:
        return None


def connector_client_with_stub(
    stub: RecordingStubRPCClient | HandlerStubRPCClient,
    *,
    lake_factory=None,
) -> ConnectorClient:
    """Build ``ConnectorClient`` that uses ``stub`` instead of real RabbitMQ."""
    return ConnectorClient(
        lake_factory=lake_factory,
        rpc_client_factory=lambda **kwargs: stub,
    )

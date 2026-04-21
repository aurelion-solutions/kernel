# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from typing import Any
import uuid
from uuid import UUID

from src.core.mq.async_rpc_client import AsyncRabbitMQRPCClient
from src.platform.storage.factory import DataLakeStorageFactory, data_lake_factory


class ConnectorClient:
    """Generic RPC transport to a connector instance (caller supplies ``instance_id`` and ``operation``).

    Accepts a shared :class:`~src.core.mq.async_rpc_client.AsyncRabbitMQRPCClient` instance
    (created once in the application lifespan).  No new connection is created per call.
    """

    def __init__(
        self,
        *,
        lake_factory: DataLakeStorageFactory | None = None,
        rpc_client: AsyncRabbitMQRPCClient | None = None,
        # Legacy: rpc_client_factory accepted for test stub injection.
        # When provided, it is called with no arguments to produce the client.
        rpc_client_factory: Any | None = None,
    ) -> None:
        self._lake_factory = lake_factory if lake_factory is not None else data_lake_factory
        if rpc_client is not None:
            self._rpc_client: Any = rpc_client
        elif rpc_client_factory is not None:
            self._rpc_client = rpc_client_factory()
        else:
            self._rpc_client = None

    @property
    def lake_factory(self) -> DataLakeStorageFactory:
        return self._lake_factory

    async def invoke(
        self,
        instance_id: str,
        operation: str,
        payload: dict[str, Any],
        *,
        result_storage_requested: bool = False,
        correlation_id: str | None = None,
        trace_parent_event_id: UUID | None = None,
        trace_initiator_type: str | None = None,
        trace_initiator_id: str | None = None,
        trace_target_type: str | None = None,
        trace_target_id: str | None = None,
    ) -> dict[str, Any]:
        if self._rpc_client is None:
            raise RuntimeError('ConnectorClient has no RPC client configured')

        cid = correlation_id if correlation_id is not None else str(uuid.uuid4())
        trace_parent_str = str(trace_parent_event_id) if trace_parent_event_id is not None else None

        result = await self._rpc_client.request(
            instance_id=instance_id,
            operation=operation,
            payload=payload,
            result_storage_requested=result_storage_requested,
            correlation_id=cid,
            trace_parent_event_id=trace_parent_str,
            trace_initiator_type=trace_initiator_type,
            trace_initiator_id=trace_initiator_id,
            trace_target_type=trace_target_type,
            trace_target_id=trace_target_id,
        )

        self._raise_if_error(result)
        return result

    def _raise_if_error(self, result: dict[str, Any]) -> None:
        status = result.get('status', 'ok')
        if status == 'ok':
            return

        error = result.get('error')
        if isinstance(error, dict):
            message = str(error.get('message') or 'Connector request failed')
        elif isinstance(error, str):
            message = error
        else:
            message = 'Connector request failed'

        raise RuntimeError(message)

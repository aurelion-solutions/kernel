# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import os
from typing import Any
import uuid
from uuid import UUID

from src.core.mq.rabbitmq import RabbitMQRPCClient
from src.platform.storage.factory import DataLakeStorageFactory, data_lake_factory


class ConnectorClient:
    """Generic RPC transport to a connector instance (caller supplies ``instance_id`` and ``operation``)."""

    def __init__(
        self,
        *,
        lake_factory: DataLakeStorageFactory | None = None,
        rpc_client_factory: type | None = None,
    ) -> None:
        self._lake_factory = lake_factory if lake_factory is not None else data_lake_factory
        self._rpc_client_factory = rpc_client_factory if rpc_client_factory is not None else RabbitMQRPCClient

    @property
    def lake_factory(self) -> DataLakeStorageFactory:
        return self._lake_factory

    def _build_rpc_client(self) -> RabbitMQRPCClient:
        host = os.environ.get('AURELION_RABBITMQ_HOST', 'localhost')
        port = int(os.environ.get('AURELION_RABBITMQ_PORT', '5672'))
        username = os.environ.get('AURELION_RABBITMQ_USERNAME', 'guest')
        password = os.environ.get('AURELION_RABBITMQ_PASSWORD', 'guest')
        commands_exchange = os.environ.get(
            'AURELION_CONNECTOR_COMMANDS_EXCHANGE',
            'aurelion.connectors.commands',
        )
        responses_exchange = os.environ.get(
            'AURELION_CONNECTOR_RESPONSES_EXCHANGE',
            'aurelion.connectors.responses',
        )

        return self._rpc_client_factory(
            host=host,
            port=port,
            commands_exchange=commands_exchange,
            responses_exchange=responses_exchange,
            username=username,
            password=password,
        )

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
        cid = correlation_id if correlation_id is not None else str(uuid.uuid4())
        trace_parent_str = str(trace_parent_event_id) if trace_parent_event_id is not None else None
        client = self._build_rpc_client()

        try:
            result = await client.request(
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
        finally:
            client.close()

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

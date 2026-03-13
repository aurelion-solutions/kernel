# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FastAPI dependencies for the connector RPC client."""

from fastapi import Request
from src.platform.connectors.client import ConnectorClient


class ConnectorClientNotConfiguredError(RuntimeError):
    """Raised when connector client was not set in app state."""


async def get_connector_client(request: Request) -> ConnectorClient:
    """Return connector client from app state."""
    client = getattr(request.app.state, 'connector_client', None)
    if client is None:
        raise ConnectorClientNotConfiguredError(
            'connector_client not configured; app lifespan must set app.state.connector_client'
        )
    return client

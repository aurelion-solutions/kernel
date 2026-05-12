# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Process-level ConnectorClient factory.

Provides module-level accessors for a ``ConnectorClient`` that can be called
from engine action handlers running outside a FastAPI request context
(e.g. platform_executor_node).

Usage in executor node ``_run()``::

    from src.platform.connectors.factory import set_process_connector_client
    set_process_connector_client(ConnectorClient(rpc_client=rpc_client))

Action handlers then call::

    from src.platform.connectors.factory import get_process_connector_client
    connector = get_process_connector_client()

These helpers raise ``RuntimeError`` if accessed before
``set_process_connector_client`` has been called, which surfaces as a hard
startup failure rather than a silent misconfiguration.

Library-module discipline: no ``get_settings()``, no ``load_dotenv()``,
no ``register_default_providers()`` at import time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.platform.connectors.client import ConnectorClient

# ---------------------------------------------------------------------------
# Module-level process state
# ---------------------------------------------------------------------------

_process_connector_client: ConnectorClient | None = None


def set_process_connector_client(client: ConnectorClient) -> None:
    """Register process-scoped ConnectorClient.  Must be called once at process start."""
    global _process_connector_client
    _process_connector_client = client


def get_process_connector_client() -> ConnectorClient:
    """Return the process-scoped ConnectorClient.

    Raises:
        RuntimeError: if ``set_process_connector_client`` has not been called.
    """
    if _process_connector_client is None:
        raise RuntimeError(
            'Process connector client not initialised. '
            'Call set_process_connector_client() before invoking connector-backed actions.'
        )
    return _process_connector_client

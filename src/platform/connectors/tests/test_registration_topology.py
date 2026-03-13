# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests that connector registration consumer targets the registry exchange topology."""

import asyncio
from unittest.mock import patch

import pytest

pytest.importorskip('pika')

from src.platform.connectors.registration_consumer import run_connector_registration_consumer


def test_run_connector_registration_consumer_uses_registry_exchange_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults must match connector publishers (registry exchange, topic, registration + heartbeat)."""
    for key in (
        'AURELION_CONNECTOR_REGISTRATION_EXCHANGE',
        'AURELION_CONNECTOR_REGISTRATION_QUEUE',
        'AURELION_CONNECTOR_REGISTRATION_BINDINGS',
    ):
        monkeypatch.delenv(key, raising=False)

    captured: dict = {}

    def capture_run(**kwargs: object) -> None:
        captured.clear()
        captured.update(kwargs)

    dummy_loop = asyncio.new_event_loop()
    try:
        with patch(
            'src.platform.connectors.registration_consumer.run_rabbitmq_consumer',
            side_effect=capture_run,
        ):
            run_connector_registration_consumer(dummy_loop)
    finally:
        dummy_loop.close()

    assert captured['exchange'] == 'aurelion.connectors.registry'
    assert captured['queue_name'] == 'aurelion.connectors.registration'
    assert captured['exchange_type'] == 'topic'
    assert captured['binding_keys'] == ['connector.registered', 'connector.heartbeat']

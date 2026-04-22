# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests that connector registration consumer targets the registry exchange topology."""

import asyncio
from unittest.mock import patch

import pytest

pytest.importorskip('pika')

from src.platform.connectors.registration_consumer import run_connector_registration_consumer


def test_run_connector_registration_consumer_threads_kwargs_to_run_rabbitmq_consumer() -> None:
    """Explicit kwargs are forwarded byte-for-byte to run_rabbitmq_consumer."""
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
            run_connector_registration_consumer(
                dummy_loop,
                host='localhost',
                port=5672,
                username='guest',
                password='guest',
                registration_exchange='aurelion.connectors.registry',
                registration_queue='aurelion.connectors.registration',
                registration_binding_keys=['connector.registered', 'connector.heartbeat'],
            )
    finally:
        dummy_loop.close()

    assert captured['exchange'] == 'aurelion.connectors.registry'
    assert captured['queue_name'] == 'aurelion.connectors.registration'
    assert captured['exchange_type'] == 'topic'
    assert captured['binding_keys'] == ['connector.registered', 'connector.heartbeat']

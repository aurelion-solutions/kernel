# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for RabbitMQ messaging. Require RabbitMQ server."""

import pytest
from src.core.mq.rabbitmq import RabbitMQEventPublisher

pytest.importorskip('pika')


def test_rabbitmq_publisher_publishes_event():
    """RabbitMQEventPublisher.publish sends event to queue."""
    try:
        publisher = RabbitMQEventPublisher(host='localhost', port=5672, queue='test_connector_events')
    except (OSError, ConnectionError, Exception) as e:
        pytest.skip(f'RabbitMQ not available: {e}')

    try:
        publisher.publish({'type': 'test', 'application': 'x', 'payload': {}})
    finally:
        publisher.close()

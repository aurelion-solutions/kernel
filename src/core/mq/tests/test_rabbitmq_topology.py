# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Unit tests for RabbitMQ consumer topology declaration (no broker required)."""

from unittest.mock import MagicMock

import pytest

pytest.importorskip('pika')

from src.core.mq.rabbitmq import declare_consumer_topology, declare_topic_exchange_fanout_queues


def test_declare_consumer_topology_registry_registration_bindings() -> None:
    """Registration flow: registry topic exchange, registration queue, routing keys."""
    channel = MagicMock()
    declare_consumer_topology(
        channel,
        exchange='aurelion.connectors.registry',
        exchange_type='topic',
        queue_name='aurelion.connectors.registration',
        binding_keys=['connector.registered', 'connector.heartbeat'],
    )

    channel.exchange_declare.assert_called_once_with(
        exchange='aurelion.connectors.registry',
        exchange_type='topic',
        durable=True,
    )
    channel.queue_declare.assert_called_once_with(
        queue='aurelion.connectors.registration',
        durable=True,
    )
    assert channel.queue_bind.call_count == 2
    channel.queue_bind.assert_any_call(
        queue='aurelion.connectors.registration',
        exchange='aurelion.connectors.registry',
        routing_key='connector.registered',
    )
    channel.queue_bind.assert_any_call(
        queue='aurelion.connectors.registration',
        exchange='aurelion.connectors.registry',
        routing_key='connector.heartbeat',
    )


def test_declare_consumer_topology_single_binding() -> None:
    channel = MagicMock()
    declare_consumer_topology(
        channel,
        exchange='ex',
        exchange_type='direct',
        queue_name='q',
        binding_keys=['rk.only'],
    )
    channel.queue_bind.assert_called_once_with(
        queue='q',
        exchange='ex',
        routing_key='rk.only',
    )


def test_declare_topic_exchange_fanout_queues_two_queues() -> None:
    """Log fan-out: one topic exchange, two queues, identical bindings each."""
    channel = MagicMock()
    declare_topic_exchange_fanout_queues(
        channel,
        exchange='aurelion.logs',
        queue_names=['aurelion.logs.siem', 'aurelion.logs.buffer'],
        binding_keys=['#'],
    )

    channel.exchange_declare.assert_called_once_with(
        exchange='aurelion.logs',
        exchange_type='topic',
        durable=True,
    )
    assert channel.queue_declare.call_count == 2
    channel.queue_declare.assert_any_call(queue='aurelion.logs.siem', durable=True)
    channel.queue_declare.assert_any_call(queue='aurelion.logs.buffer', durable=True)
    assert channel.queue_bind.call_count == 2
    channel.queue_bind.assert_any_call(
        queue='aurelion.logs.siem',
        exchange='aurelion.logs',
        routing_key='#',
    )
    channel.queue_bind.assert_any_call(
        queue='aurelion.logs.buffer',
        exchange='aurelion.logs',
        routing_key='#',
    )

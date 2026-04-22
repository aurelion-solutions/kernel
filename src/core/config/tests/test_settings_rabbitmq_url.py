# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Settings.rabbitmq_url and RabbitMQ field defaults contract tests."""

import pytest
from src.core.config import Settings

_DB = 'postgresql+asyncpg://postgres:test@localhost/test'

_RABBITMQ_ENVVARS = (
    'RABBITMQ_HOST',
    'RABBITMQ_PORT',
    'RABBITMQ_USERNAME',
    'RABBITMQ_PASSWORD',
    'RABBITMQ_EVENTS_EXCHANGE',
    'RABBITMQ_LOGS_EXCHANGE',
    'RABBITMQ_CONNECTOR_COMMANDS_EXCHANGE',
    'RABBITMQ_CONNECTOR_RESPONSES_EXCHANGE',
)


def _clear_rabbitmq_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all RabbitMQ env vars so Settings() kwargs are authoritative."""
    for key in _RABBITMQ_ENVVARS:
        monkeypatch.delenv(key, raising=False)


def test_rabbitmq_url_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default rabbitmq_url uses guest credentials and localhost:5672."""
    _clear_rabbitmq_env(monkeypatch)
    s = Settings(database_url=_DB)
    assert s.rabbitmq_url == 'amqp://guest:guest@localhost:5672/'


def test_rabbitmq_url_honours_field_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    """rabbitmq_url reflects custom host/port/username/password."""
    _clear_rabbitmq_env(monkeypatch)
    s = Settings(
        database_url=_DB,
        rabbitmq_host='mq.example.com',
        rabbitmq_port=5673,
        rabbitmq_username='alice',
        rabbitmq_password='s3cr3t',
    )
    assert s.rabbitmq_url == 'amqp://alice:s3cr3t@mq.example.com:5673/'


def test_rabbitmq_exchange_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """All four exchange name fields carry the expected defaults."""
    _clear_rabbitmq_env(monkeypatch)
    s = Settings(database_url=_DB)
    assert s.rabbitmq_events_exchange == 'aurelion.events'
    assert s.rabbitmq_logs_exchange == 'aurelion.logs'
    assert s.rabbitmq_connector_commands_exchange == 'aurelion.connectors.commands'
    assert s.rabbitmq_connector_responses_exchange == 'aurelion.connectors.responses'

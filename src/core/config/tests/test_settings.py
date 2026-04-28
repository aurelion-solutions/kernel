# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for the new typed config bootstrap layer."""

from __future__ import annotations

import json

import pytest
from src.core.config.loader import load_settings
from src.core.config.settings import (
    AppSettings,
    PostgresSettings,
    RabbitMQSettings,
    Settings,
)

# ---------------------------------------------------------------------------
# Fake SecretManager
# ---------------------------------------------------------------------------


class FakeSecretManager:
    """In-memory ConfigSecretManager for tests."""

    def __init__(self, data: dict[str, str]) -> None:
        self._data = data

    def get_secret(self, key: str) -> str:
        if key not in self._data:
            raise KeyError(f'Secret not found: {key!r}')
        return self._data[key]


def _postgres_secret(**overrides) -> str:
    base = {
        'host': 'localhost',
        'port': 5432,
        'database': 'aurelion',
        'username': 'aurelion',
        'password': 'aurelion',
    }
    return json.dumps({**base, **overrides})


def _rabbitmq_secret(**overrides) -> str:
    base = {
        'host': 'localhost',
        'port': 5672,
        'username': 'guest',
        'password': 'guest',
        'events_exchange': 'aurelion.events',
        'logs_exchange': 'aurelion.logs',
        'connector_commands_exchange': 'aurelion.connectors.commands',
        'connector_responses_exchange': 'aurelion.connectors.responses',
    }
    return json.dumps({**base, **overrides})


# ---------------------------------------------------------------------------
# PostgresSettings.dsn
# ---------------------------------------------------------------------------


def test_postgres_dsn_format() -> None:
    pg = PostgresSettings(
        host='db.example.com',
        port=5432,
        database='mydb',
        username='alice',
        password='s3cr3t',  # type: ignore[arg-type]
    )
    assert pg.dsn == 'postgresql+asyncpg://alice:s3cr3t@db.example.com:5432/mydb'


def test_postgres_dsn_escapes_special_chars() -> None:
    pg = PostgresSettings(password='p@ss:w/ord#1')  # type: ignore[arg-type]
    assert pg.dsn == 'postgresql+asyncpg://aurelion:p%40ss%3Aw%2Ford%231@localhost:5432/aurelion'


def test_postgres_catalog_dsn_format() -> None:
    pg = PostgresSettings(
        host='localhost',
        port=5432,
        database='aurelion',
        username='aurelion',
        password='aurelion',  # type: ignore[arg-type]
    )
    assert 'iceberg_catalog' in pg.catalog_dsn
    assert pg.catalog_dsn.startswith('postgresql+psycopg2://')


# ---------------------------------------------------------------------------
# RabbitMQSettings.url
# ---------------------------------------------------------------------------


def test_rabbitmq_url_default() -> None:
    mq = RabbitMQSettings()
    assert mq.url == 'amqp://guest:guest@localhost:5672/'


def test_rabbitmq_url_custom() -> None:
    mq = RabbitMQSettings(
        host='mq.example.com',
        port=5673,
        username='alice',
        password='s3cr3t',  # type: ignore[arg-type]
    )
    assert mq.url == 'amqp://alice:s3cr3t@mq.example.com:5673/'


def test_rabbitmq_url_escapes_special_chars() -> None:
    mq = RabbitMQSettings(password='p@ss:w/ord#1')  # type: ignore[arg-type]
    assert '@' not in mq.url.split('@', 1)[1]  # only one @ separates creds from host
    assert mq.url == 'amqp://guest:p%40ss%3Aw%2Ford%231@localhost:5672/'


def test_rabbitmq_exchange_defaults() -> None:
    mq = RabbitMQSettings()
    assert mq.events_exchange == 'aurelion.events'
    assert mq.logs_exchange == 'aurelion.logs'
    assert mq.connector_commands_exchange == 'aurelion.connectors.commands'
    assert mq.connector_responses_exchange == 'aurelion.connectors.responses'


# ---------------------------------------------------------------------------
# AppSettings CORS validator
# ---------------------------------------------------------------------------


def test_cors_validator_parses_csv_string() -> None:
    app = AppSettings(cors_allow_origins='http://a.com,http://b.com')  # type: ignore[arg-type]
    assert app.cors_allow_origins == ['http://a.com', 'http://b.com']


def test_cors_validator_accepts_list() -> None:
    app = AppSettings(cors_allow_origins=['http://a.com', 'http://b.com'])
    assert app.cors_allow_origins == ['http://a.com', 'http://b.com']


def test_cors_validator_wildcard() -> None:
    app = AppSettings(cors_allow_origins='*')  # type: ignore[arg-type]
    assert app.cors_allow_origins == ['*']


def test_cors_validator_none_returns_empty() -> None:
    app = AppSettings(cors_allow_origins=None)  # type: ignore[arg-type]
    assert app.cors_allow_origins == []


# ---------------------------------------------------------------------------
# load_settings — full path
# ---------------------------------------------------------------------------


def test_load_settings_happy_path() -> None:
    sm = FakeSecretManager(
        {
            'postgres': _postgres_secret(),
            'rabbitmq': _rabbitmq_secret(),
        }
    )
    settings = load_settings(sm)
    assert isinstance(settings, Settings)
    assert settings.postgres.host == 'localhost'
    assert settings.rabbitmq.host == 'localhost'


def test_load_settings_missing_required_key_raises() -> None:
    sm = FakeSecretManager({'postgres': _postgres_secret()})
    with pytest.raises(KeyError):
        load_settings(sm)


def test_load_settings_optional_key_fallback() -> None:
    """When 'app' and 'lake' keys are absent, defaults apply."""
    sm = FakeSecretManager(
        {
            'postgres': _postgres_secret(),
            'rabbitmq': _rabbitmq_secret(),
        }
    )
    settings = load_settings(sm)
    assert settings.app.cors_allow_origins == ['*']
    assert settings.app.debug is False
    assert settings.lake.catalog_name == 'aurelion'


def test_load_settings_custom_app_key() -> None:
    sm = FakeSecretManager(
        {
            'postgres': _postgres_secret(),
            'rabbitmq': _rabbitmq_secret(),
            'app': json.dumps({'cors_allow_origins': 'http://ui.example.com', 'debug': True}),
        }
    )
    settings = load_settings(sm)
    assert settings.app.cors_allow_origins == ['http://ui.example.com']
    assert settings.app.debug is True

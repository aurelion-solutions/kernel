# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Typed bootstrap configuration hierarchy.

All classes are pure ``BaseModel`` — no pydantic-settings, no env reads,
no file I/O.  Values are supplied by ``loader.load_settings`` which pulls
them from a ``ConfigSecretManager``.

Layer placement: ``src.core.config`` (core layer).  No platform imports.
"""

from __future__ import annotations

from urllib.parse import quote

from pydantic import BaseModel, Field, SecretStr, field_validator


class PostgresSettings(BaseModel):
    """PostgreSQL connection settings."""

    host: str = 'localhost'
    port: int = 5432
    database: str = 'aurelion'
    username: str = 'aurelion'
    password: SecretStr = SecretStr('aurelion')

    @property
    def dsn(self) -> str:
        """asyncpg-compatible DSN: ``postgresql+asyncpg://...``."""
        user = quote(self.username, safe='')
        pwd = quote(self.password.get_secret_value(), safe='')
        return f'postgresql+asyncpg://{user}:{pwd}@{self.host}:{self.port}/{self.database}'

    @property
    def catalog_dsn(self) -> str:
        """psycopg2 DSN pointing at the iceberg_catalog schema.

        Used by PyIceberg SQL catalog and DuckDB postgres_scanner.
        """
        user = quote(self.username, safe='')
        pwd = quote(self.password.get_secret_value(), safe='')
        return (
            f'postgresql+psycopg2://{user}:{pwd}'
            f'@{self.host}:{self.port}/{self.database}'
            '?options=-csearch_path%3Diceberg_catalog'
        )


class RabbitMQSettings(BaseModel):
    """RabbitMQ connection settings."""

    host: str = 'localhost'
    port: int = 5672
    username: str = 'guest'
    password: SecretStr = SecretStr('guest')
    events_exchange: str = 'aurelion.events'
    logs_exchange: str = 'aurelion.logs'
    connector_commands_exchange: str = 'aurelion.connectors.commands'
    connector_responses_exchange: str = 'aurelion.connectors.responses'

    @property
    def url(self) -> str:
        """AMQP URL for pika / aio_pika consumers."""
        user = quote(self.username, safe='')
        pwd = quote(self.password.get_secret_value(), safe='')
        return f'amqp://{user}:{pwd}@{self.host}:{self.port}/'


class AppSettings(BaseModel):
    """Bootstrap application settings.

    These fields are intentionally kept in the bootstrap layer (secrets file),
    NOT in RuntimeSettings (DB).  Reason: CORSMiddleware must be registered at
    app creation time, before the lifespan runs and before the DB is reachable.
    Moving them to DB would require a custom per-request CORS middleware —
    an added complexity not justified at this stage.

    If runtime-tuneable CORS becomes a requirement, implement a middleware that
    reads from app.state.runtime_settings and remove these fields from here.
    """

    cors_allow_origins: list[str] = ['*']
    debug: bool = False

    @field_validator('cors_allow_origins', mode='before')
    @classmethod
    def _parse_cors(cls, v: object) -> list[str]:
        """Accept a CSV string or a list.  Ops who pass ``"*,foo.com"`` are safe."""
        if v is None:
            return []
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            s = v.strip()
            if not s:
                return []
            return [p.strip() for p in s.split(',') if p.strip()]
        return []


class LakeStaticSettings(BaseModel):
    """Deployment-time lake settings (warehouse location, backend choice).

    Operational knobs (pool_size, read_page_size, etc.) live in
    ``RuntimeSettingsConfig`` and are applied in ``build_lake_settings``.
    """

    catalog_name: str = 'aurelion'
    warehouse_uri: str = 'file:///var/lib/aurelion/warehouse'
    storage_provider: str = 'file'
    artifacts_write_backend: str = 'iceberg'


class Settings(BaseModel):
    """Root bootstrap configuration snapshot.

    Immutable after construction.  Created once per process by
    ``get_settings()`` and cached via ``lru_cache``.
    """

    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    rabbitmq: RabbitMQSettings = Field(default_factory=RabbitMQSettings)
    app: AppSettings = Field(default_factory=AppSettings)
    lake: LakeStaticSettings = Field(default_factory=LakeStaticSettings)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Bootstrap config loader.

Reads secrets from a ``ConfigSecretManager`` and constructs an immutable
``Settings`` object.  No environment reads happen here — that is the
responsibility of the calling entrypoint (which calls ``load_dotenv()``
before constructing the secret manager).
"""

from __future__ import annotations

import json

from src.core.config.settings import (
    AppSettings,
    LakeStaticSettings,
    PostgresSettings,
    RabbitMQSettings,
    Settings,
)
from src.core.secrets.interface import ConfigSecretManager


def _parse_optional_json(sm: ConfigSecretManager, key: str) -> dict | None:
    """Return parsed JSON from secret *key*, or ``None`` if key is absent."""
    try:
        raw = sm.get_secret(key)
    except KeyError:
        return None
    return json.loads(raw)


def load_settings(sm: ConfigSecretManager) -> Settings:
    """Construct ``Settings`` from secrets in *sm*.

    Required keys: ``postgres``, ``rabbitmq``.
    Optional keys: ``app``, ``lake`` (defaults apply on ``KeyError``).

    Each secret value must be a JSON-encoded dict matching the corresponding
    settings model fields.

    Raises
    ------
    KeyError
        When a required secret key is absent.
    json.JSONDecodeError
        When a secret value is not valid JSON.
    pydantic.ValidationError
        When the parsed dict does not match the model.
    """
    postgres_raw = json.loads(sm.get_secret('postgres'))
    rabbitmq_raw = json.loads(sm.get_secret('rabbitmq'))
    lake_raw = _parse_optional_json(sm, 'lake')
    app_raw = _parse_optional_json(sm, 'app')

    return Settings(
        postgres=PostgresSettings.model_validate(postgres_raw),
        rabbitmq=RabbitMQSettings.model_validate(rabbitmq_raw),
        lake=LakeStaticSettings.model_validate(lake_raw) if lake_raw is not None else LakeStaticSettings(),
        app=AppSettings.model_validate(app_raw) if app_raw is not None else AppSettings(),
    )

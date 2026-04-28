# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""RuntimeSettingsService — reads and writes operator-tunable knobs.

Design notes
------------
``ensure_defaults()`` uses ``INSERT ... ON CONFLICT DO NOTHING`` so
concurrent startups in a k8s cluster are safe: the first writer wins and
subsequent writers skip the row without error.

Transaction ownership follows the project convention:
- Service methods flush; the caller (lifespan, route handler) commits.
- No ``session.commit()`` calls inside this class.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from src.platform.runtime_settings.models import RuntimeSetting
from src.platform.runtime_settings.schemas import (
    RuntimeSettingsConfig,
    RuntimeSettingUpdate,
)

# ---------------------------------------------------------------------------
# Module-level helpers (business logic; do not belong in schemas.py)
# ---------------------------------------------------------------------------

# Mapping from RuntimeSetting.key → (field_name, value_type_hint).
# Used by ensure_defaults() to seed initial rows and by load() to coerce types.
#
# TODO(tech-debt): FIELD_META duplicates RuntimeSettingsConfig — field names,
# types, and defaults exist in two places. Adding a new knob requires updating
# both this dict and RuntimeSettingsConfig, and tests will catch the mismatch
# only on the next run. Refactor to a single SettingDefinition registry that
# drives both the Pydantic model (via create_model) and this metadata.
FIELD_META: dict[str, tuple[str, str]] = {
    'log_buffer_retention_seconds': ('log_buffer_retention_seconds', 'int'),
    'app_name': ('app_name', 'str'),
    'lake_pool_size': ('lake_pool_size', 'int'),
    'lake_acquire_timeout_seconds': ('lake_acquire_timeout_seconds', 'float'),
    'lake_pg_any_array_max_size': ('lake_pg_any_array_max_size', 'int'),
    'lake_read_page_size': ('lake_read_page_size', 'int'),
    'llm_max_loaded_models': ('llm_max_loaded_models', 'int'),
    'llm_max_messages': ('llm_max_messages', 'int'),
    'llm_max_chars_per_message': ('llm_max_chars_per_message', 'int'),
    'llm_max_total_chars': ('llm_max_total_chars', 'int'),
}


def coerce_runtime_value(value_str: str, value_type: str) -> object:
    """Coerce *value_str* to the Python type indicated by *value_type*."""
    if value_type == 'int':
        return int(value_str)
    if value_type == 'float':
        return float(value_str)
    return value_str


class InvalidRuntimeSettingValueError(ValueError):
    """Raised when a value fails type coercion or Pydantic constraint validation."""


if TYPE_CHECKING:
    from src.platform.logs.service import LogService


class RuntimeSettingsService:
    """Service for reading and updating runtime settings.

    Parameters
    ----------
    session:
        Active async SQLAlchemy session.  Service flushes; caller commits.
    log_service:
        Log service instance injected by the caller.  Pass
        ``noop_log_service`` in tests.
    """

    def __init__(self, session: AsyncSession, log_service: LogService) -> None:
        self._session = session
        self._log_service = log_service

    async def ensure_defaults(self) -> int:
        """Insert default rows for all known keys if they do not exist yet.

        Uses ``INSERT ... ON CONFLICT DO NOTHING`` — idempotent and safe
        under concurrent startups.

        Returns the number of rows actually inserted (0 on a warm start).
        Flushes the session; the caller must commit.
        """
        defaults = RuntimeSettingsConfig()
        inserted = 0
        for key, (field_name, value_type) in FIELD_META.items():
            default_value = str(getattr(defaults, field_name))
            result = await self._session.execute(
                text(
                    'INSERT INTO runtime_settings (key, value, value_type, updated_at) '
                    'VALUES (:key, :value, :value_type, NOW()) '
                    'ON CONFLICT (key) DO NOTHING '
                    'RETURNING key'
                ),
                {'key': key, 'value': default_value, 'value_type': value_type},
            )
            if result.fetchone() is not None:
                inserted += 1

        await self._session.flush()

        self._log_service.emit_safe(
            level=_info_level(),
            message='runtime_settings.ensure_defaults',
            component='platform.runtime_settings',
            payload=_system_payload({'keys_inserted': inserted}),
        )
        return inserted

    async def load(self) -> RuntimeSettingsConfig:
        """Read all known keys from DB and return a typed snapshot.

        If a key is absent (e.g. a freshly added key not yet seeded), the
        typed default from ``RuntimeSettingsConfig`` is used — no ``KeyError``
        will escape to callers mid-request.
        """
        from sqlalchemy import select  # noqa: PLC0415 — keep import local

        result = await self._session.execute(select(RuntimeSetting))
        rows: dict[str, str] = {}
        types: dict[str, str] = {}
        for row in result.scalars():
            rows[row.key] = row.value
            types[row.key] = row.value_type

        overrides: dict[str, object] = {}
        for key, (field_name, value_type) in FIELD_META.items():
            if key in rows:
                overrides[field_name] = coerce_runtime_value(rows[key], types.get(key, value_type))

        return RuntimeSettingsConfig.model_validate(overrides)

    async def list_all(self) -> list[RuntimeSetting]:
        """Return all runtime_settings rows ordered by key."""
        from sqlalchemy import select  # noqa: PLC0415

        result = await self._session.execute(select(RuntimeSetting).order_by(RuntimeSetting.key))
        return list(result.scalars())

    async def get(self, key: str) -> RuntimeSetting | None:
        """Return a single row by primary key, or ``None`` if absent."""
        return await self._session.get(RuntimeSetting, key)

    async def update(self, key: str, payload: RuntimeSettingUpdate) -> RuntimeSetting:
        """Update the value of an existing setting.

        Validates before writing: coerces the string value to the stored
        value_type, then runs it through RuntimeSettingsConfig.model_validate
        so Pydantic field constraints (ge, le, etc.) are enforced.

        Raises
        ------
        KeyError
            When *key* does not exist in the database.
        InvalidRuntimeSettingValueError
            When the value cannot be coerced to the expected type or fails
            a Pydantic constraint (e.g. lake_read_page_size > 5000).

        Flushes the session; the caller must commit.
        Emits one INFO log event with old and new values.
        """
        from pydantic import ValidationError  # noqa: PLC0415

        row = await self._session.get(RuntimeSetting, key)
        if row is None:
            raise KeyError(f'Runtime setting not found: {key!r}')

        try:
            coerced = coerce_runtime_value(payload.value, row.value_type)
        except (ValueError, TypeError) as exc:
            raise InvalidRuntimeSettingValueError(
                f'Cannot coerce {payload.value!r} to {row.value_type!r} for key {key!r}: {exc}'
            ) from exc

        field_name = FIELD_META[key][0]
        candidate = await self.load()
        data = candidate.model_dump()
        data[field_name] = coerced
        try:
            RuntimeSettingsConfig.model_validate(data)
        except ValidationError as exc:
            raise InvalidRuntimeSettingValueError(
                f'Value {payload.value!r} for key {key!r} failed validation: {exc}'
            ) from exc

        old_value = row.value
        row.value = payload.value

        await self._session.flush()

        self._log_service.emit_safe(
            level=_info_level(),
            message='runtime_setting.updated',
            component='platform.runtime_settings',
            payload=_system_payload(
                {
                    'key': key,
                    'old_value': old_value,
                    'new_value': payload.value,
                    'value_type': row.value_type,
                }
            ),
        )
        return row


# ---------------------------------------------------------------------------
# Private helpers (module-level per project convention)
# ---------------------------------------------------------------------------


def _info_level() -> object:
    """Return LogLevel.INFO without importing at module level."""
    from src.platform.logs.schemas import LogLevel  # noqa: PLC0415

    return LogLevel.INFO


def _system_payload(extra: dict) -> dict:
    """Build a payload dict with system participant fields."""
    return {
        'initiator_type': 'system',
        'initiator_id': 'platform',
        'actor_type': 'system',
        'actor_id': 'platform.runtime_settings',
        'target_type': 'system',
        'target_id': 'runtime_settings',
        **extra,
    }

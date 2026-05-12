# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic v2 schemas for the runtime_settings slice."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class RuntimeSettingRead(BaseModel):
    """API response schema for a single runtime setting."""

    key: str
    value: str
    value_type: str
    updated_at: datetime

    model_config = {'from_attributes': True}


class RuntimeSettingUpdate(BaseModel):
    """Request body for PUT /runtime-settings/{key}."""

    value: str
    """String-serialized value.  The caller is responsible for valid serialization.
    The service coerces the string to the declared value_type and validates it
    against RuntimeSettingsConfig field constraints before persisting.
    """


class RuntimeSettingsConfig(BaseModel):
    """Typed snapshot of all runtime settings.

    Defaults are used when a key is absent from the database (e.g. after a
    fresh deployment before ``ensure_defaults`` has run, or during tests).
    """

    log_buffer_retention_seconds: int = 3600
    app_name: str = 'Aurelion'
    lake_pool_size: int = 4
    lake_acquire_timeout_seconds: float = 5.0
    lake_pg_any_array_max_size: int = 25000
    lake_read_page_size: int = Field(default=1000, ge=1, le=5000)
    reconciliation_fetch_batch_size: int = Field(default=5000, ge=1, le=50000)
    llm_max_loaded_models: int = Field(default=2, ge=1)
    llm_max_messages: int = Field(default=32, ge=1)
    llm_max_chars_per_message: int = Field(default=32000, ge=1)
    llm_max_total_chars: int = Field(default=128000, ge=1)
    safe_revoke_threshold: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            'Fraction of existing effective facts that may be revoked in a single plan '
            'before requires_confirmation is set to True. Default 0.5 (50%).'
        ),
    )
    max_apply_duration_seconds: int = Field(
        default=3600,
        ge=60,
        description=(
            'Maximum expected duration (in seconds) for an access_apply pipeline run. '
            'access_apply_active rows older than this threshold are considered stale '
            'and deleted by the cleanup scanner regardless of pipeline run status. '
            'Default 3600 (1 hour).'
        ),
    )
    scanner_window_lookback_seconds: int = Field(
        default=120,
        ge=1,
        description=(
            'Look-back window in seconds for the scheduled-replan scanner (E4). '
            'The scanner queries initiatives whose valid_from or valid_until falls in '
            '[now() - lookback, now() + 60s]. Default 120 (2 minutes). '
            'Tests override via DI to keep fixtures fast.'
        ),
    )

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    app_name: str = 'FastAPI App'
    debug: bool = False
    database_url: str

    #: Max age of rows in ``log_event_buffer`` (by event ``timestamp``) before cleanup deletes them.
    log_buffer_retention_seconds: int = Field(default=3600, ge=1)

    cors_allow_origins: list[str] = ['*']

    @field_validator('cors_allow_origins', mode='before')
    @classmethod
    def _parse_cors_allow_origins(cls, v):
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

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / '.env',
        extra='ignore',
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (
            env_settings,
            dotenv_settings,
            init_settings,
            file_secret_settings,
        )


settings = Settings()

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake infrastructure settings (pure BaseModel, no pydantic-settings)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from src.core.config.settings import PostgresSettings
    from src.platform.runtime_settings.schemas import RuntimeSettingsConfig


class LakeSettings(BaseModel):
    """Settings for the lake infrastructure slice.

    Constructed by ``build_lake_settings`` from bootstrap ``Settings`` and
    ``RuntimeSettingsConfig``.  Not a BaseSettings — no env reads.

    ``catalog_url`` defaults to a localhost dev URL.  In production it is
    always supplied by ``build_lake_settings`` from ``postgres.catalog_dsn``.
    Tests that need an isolated catalog pass a sqlite:// URL.
    """

    catalog_url: str = (
        'postgresql+psycopg2://postgres:postgres@localhost:5432/aurelion?options=-csearch_path%3Diceberg_catalog'
    )
    """SQLAlchemy URL pointing at the kernel PG with iceberg_catalog schema."""

    catalog_name: str = 'aurelion'
    """PyIceberg catalog logical name."""

    warehouse_uri: str = 'file:///var/lib/aurelion/warehouse'
    """Storage URI for the warehouse (file:// in dev, s3:// in prod)."""

    storage_provider: Literal['file', 's3'] = 'file'
    """Storage backend: file (local FS) or s3 (SeaweedFS / AWS S3)."""

    pool_size: int = 4
    """DuckDB session pool capacity."""

    acquire_timeout_seconds: float = 5.0
    """Seconds to wait for a pool slot before raising LakeSessionPoolExhaustedError."""

    artifacts_write_backend: Literal['pg', 'iceberg'] = 'iceberg'
    """Default flipped to iceberg in Phase 15 Step 16 (DROP TABLE access_artifacts).
    The 'pg' literal is retained only so a misconfigured deployment fails fast
    (raises AccessArtifactLakeNotConfiguredError) instead of silently corrupting state."""

    pg_any_array_max_size: int = 25000
    """Max IDs to pass via ANY($1::uuid[]) before switching to TEMP TABLE unnest pattern."""

    read_page_size: int = Field(default=1000, ge=1, le=5000)
    """Default page size for cursor-paginated lake reads. Maximum 5000."""

    reconciliation_fetch_batch_size: int = Field(default=5000, ge=1, le=50000)
    """Batch size for DuckDB fetchmany iterations during reconciliation. Default 5000."""


def build_lake_settings(
    postgres: PostgresSettings,
    runtime: RuntimeSettingsConfig,
    *,
    catalog_name: str = 'aurelion',
    warehouse_uri: str = 'file:///var/lib/aurelion/warehouse',
    storage_provider: Literal['file', 's3'] = 'file',
    artifacts_write_backend: Literal['pg', 'iceberg'] = 'iceberg',
) -> LakeSettings:
    """Build ``LakeSettings`` from bootstrap and runtime config.

    Deployment-time values (catalog_name, warehouse_uri, storage_provider,
    artifacts_write_backend) come from ``Settings.lake`` via keyword args.
    Operational knobs (pool_size, acquire_timeout_seconds,
    pg_any_array_max_size, read_page_size) come from ``runtime``.
    The ``catalog_url`` is derived from ``postgres.catalog_dsn``.
    """
    return LakeSettings(
        catalog_url=postgres.catalog_dsn,
        catalog_name=catalog_name,
        warehouse_uri=warehouse_uri,
        storage_provider=storage_provider,
        artifacts_write_backend=artifacts_write_backend,
        pool_size=runtime.lake_pool_size,
        acquire_timeout_seconds=runtime.lake_acquire_timeout_seconds,
        pg_any_array_max_size=runtime.lake_pg_any_array_max_size,
        read_page_size=runtime.lake_read_page_size,
        reconciliation_fetch_batch_size=runtime.reconciliation_fetch_batch_size,
    )

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic v2 schemas for the lake_migration slice."""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, ConfigDict, Field
from src.engines.lake_migration.models import LakeMigrationDataset, LakeMigrationStatus


class LakeMigrationStartRequest(BaseModel):
    """Body for POST /api/v0/lake-migrations."""

    dataset: str = Field(
        description="Dataset to migrate: 'access_artifacts', 'access_facts', or 'all'.",
    )
    batch_size: int = Field(
        default=5000,
        ge=1,
        le=50000,
        description='Rows per batch (5000 default, max 50000).',
    )


class LakeMigrationRunRead(BaseModel):
    """Response schema for a single migration run."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    dataset: LakeMigrationDataset
    status: LakeMigrationStatus
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    last_processed_id: uuid.UUID | None = None
    rows_read: int
    rows_written: int
    batch_size: int
    error: str | None = None
    synthetic_run_id: uuid.UUID | None = None
    lake_batch_id: uuid.UUID
    metadata_json: dict[str, Any] | None = None


class LakeMigrationRunList(BaseModel):
    """Paginated list of migration runs."""

    items: list[LakeMigrationRunRead]
    next_cursor: str | None = None

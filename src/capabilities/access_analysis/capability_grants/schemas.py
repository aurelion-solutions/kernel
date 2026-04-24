# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic schemas for the CapabilityGrant slice."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CapabilityGrantRead(BaseModel):
    """Read schema for CapabilityGrant. from_attributes=True for ORM → schema conversion."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    subject_id: UUID
    capability_id: int
    scope_key_id: int
    scope_value: str | None
    application_id: UUID
    source_effective_grant_id: UUID
    source_capability_mapping_id: int
    observed_at: datetime
    tombstoned_at: datetime | None


class ProjectionRunSummary(BaseModel):
    """Summary returned by CapabilityProjectionService methods."""

    scope_kind: Literal['effective_grant', 'application']
    scope_id: UUID
    pairs_projected: int
    rows_upserted: int
    rows_inserted: int
    rows_updated: int
    rows_tombstoned: int
    started_at: datetime
    finished_at: datetime

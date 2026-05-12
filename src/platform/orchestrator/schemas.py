# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic schemas for the pipeline orchestrator REST API (Step 11)."""

from __future__ import annotations

import datetime
from typing import Any, Literal
import uuid

from pydantic import BaseModel, ConfigDict
from src.platform.orchestrator.models import PipelineRunStatus, StepRunStatus

# ---------------------------------------------------------------------------
# Pipeline definition schemas (read from loader cache, not DB)
# ---------------------------------------------------------------------------


class PipelineTriggerSpec(BaseModel):
    """Serialised form of one trigger entry from a pipeline definition."""

    type: str
    routing_key: str | None = None
    cron: str | None = None
    every: str | None = None
    match: dict[str, Any] | None = None
    args: dict[str, Any] | None = None


class PipelineSummary(BaseModel):
    """Summary of a loaded pipeline definition — no step details."""

    name: str
    version: int
    schema_version: int
    description: str | None
    step_count: int
    triggers: list[PipelineTriggerSpec]


class PipelineDetail(PipelineSummary):
    """Full pipeline definition detail — superset of PipelineSummary."""

    args_schema: dict[str, Any]
    steps: list[dict[str, Any]]
    content_hash: str
    source_path: str


# ---------------------------------------------------------------------------
# Pipeline run / step run schemas (read from DB)
# ---------------------------------------------------------------------------


class StepRunSummary(BaseModel):
    """Compact step run row — no args/result."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_name: str
    attempt: int
    status: StepRunStatus
    started_at: datetime.datetime | None
    finished_at: datetime.datetime | None
    error: str | None


class StepRunDetail(StepRunSummary):
    """Full step run row — includes args and result."""

    args: dict[str, Any]
    result: dict[str, Any] | None


class PipelineRunSummary(BaseModel):
    """Compact pipeline run row — no args or step_runs."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    pipeline_name: str
    pipeline_version: int
    content_hash: str
    status: PipelineRunStatus
    trigger_source: str
    current_step: str | None
    started_at: datetime.datetime | None
    finished_at: datetime.datetime | None
    error: str | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


class PipelineRunDetail(PipelineRunSummary):
    """Full pipeline run row — includes args and ordered step runs."""

    args: dict[str, Any]
    steps: list[StepRunSummary]


# ---------------------------------------------------------------------------
# Request / response schemas for POST /pipeline-runs
# ---------------------------------------------------------------------------


class CreatePipelineRunRequest(BaseModel):
    """Body for POST /pipeline-runs.

    ``trigger_source`` is NOT a body field — the handler hard-codes
    ``PipelineTriggerSource.http`` (manual-trigger marker; YAML triggers cannot
    declare 'http').
    """

    pipeline_name: str
    pipeline_version: int | None = None
    args: dict[str, Any] = {}


class CreatePipelineRunResponse(BaseModel):
    """Response body for POST /pipeline-runs (201 fresh, 200 existing)."""

    pipeline_run_id: uuid.UUID
    status: PipelineRunStatus
    pipeline_version: int
    created: bool


# ---------------------------------------------------------------------------
# Cancel response schema
# ---------------------------------------------------------------------------


class RetryPipelineRunResponse(BaseModel):
    """Response body for POST /pipeline-runs/{run_id}/retry (always 201)."""

    run_id: uuid.UUID
    retry_of_run_id: uuid.UUID
    status: PipelineRunStatus
    pipeline_name: str
    pipeline_version: int


class CancelPipelineRunResponse(BaseModel):
    """Response body for POST /pipeline-runs/{run_id}/cancel.

    ``status`` is either 'cancelled' (sync — run was pending/awaiting_event) or
    'cancelling' (async — runner watcher owns the terminal transition).
    """

    run_id: uuid.UUID
    status: Literal[PipelineRunStatus.cancelled, PipelineRunStatus.cancelling]


# ---------------------------------------------------------------------------
# Well-known / discovery schemas
# ---------------------------------------------------------------------------


class ActionDescriptor(BaseModel):
    """Description of a single registered engine action for the catalogue endpoint."""

    engine: str
    action: str
    idempotent: bool
    args_schema: dict[str, Any]
    result_schema: dict[str, Any]

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Ingest API schemas for connector results."""

from typing import Any, Literal
import uuid

from pydantic import BaseModel, Field, model_validator
from src.inventory.access_artifacts.schemas import AccessArtifactBulkItem


class LakeRefLocation(BaseModel):
    """Location for lake_ref result type; points to data in the lake."""

    provider: str = Field(..., min_length=1, description='Storage provider (e.g. file)')
    storage_key: str = Field(..., min_length=1, description='Storage key (e.g. dataset/path)')
    batch_id: str | None = Field(default=None, description='Optional lake batch ID')


ResultType = Literal['inline', 'lake_ref', 'artifacts_bulk']


class ArtifactsBulkPayload(BaseModel):
    """Payload for result_type='artifacts_bulk' connector results."""

    ingest_batch_id: uuid.UUID
    application_id: uuid.UUID
    items: list[AccessArtifactBulkItem] = Field(min_length=1, max_length=10_000)


class ConnectorResultIngestRequest(BaseModel):
    """Unified connector result envelope for ingest."""

    task_id: str = Field(..., description='Task identifier')
    application_id: str = Field(..., description='Application identifier')
    operation: str = Field(..., min_length=1, max_length=64, description='Operation name')
    status: str = Field(..., min_length=1, max_length=64, description='Result status')
    result_type: ResultType = Field(..., description='inline (payload), lake_ref (location), or artifacts_bulk')
    result_id: str = Field(..., description='Result identifier')
    code: str | None = Field(default=None, description='Optional status code')
    payload: dict[str, Any] | None = Field(default=None, description='Result data (required for inline/artifacts_bulk)')
    location: LakeRefLocation | None = Field(default=None, description='Lake reference (required for lake_ref)')

    @model_validator(mode='after')
    def validate_result_type_requirements(self) -> 'ConnectorResultIngestRequest':
        if self.result_type == 'inline':
            if self.payload is None:
                raise ValueError('inline result_type requires payload')
        elif self.result_type == 'lake_ref':
            if self.location is None:
                raise ValueError('lake_ref result_type requires location')
        elif self.result_type == 'artifacts_bulk':
            if self.payload is None:
                raise ValueError('artifacts_bulk result_type requires payload')
        return self


class ConnectorResultIngestResponse(BaseModel):
    """Response for connector result ingest (staging row persisted)."""

    task_id: str
    result_id: str
    operation: str
    status: str

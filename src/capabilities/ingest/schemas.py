# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Ingest API schemas for connector results."""

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class LakeRefLocation(BaseModel):
    """Location for lake_ref result type; points to data in the lake."""

    provider: str = Field(..., min_length=1, description='Storage provider (e.g. file)')
    storage_key: str = Field(..., min_length=1, description='Storage key (e.g. dataset/path)')
    batch_id: str | None = Field(default=None, description='Optional lake batch ID')


ResultType = Literal['inline', 'lake_ref']


class ConnectorResultIngestRequest(BaseModel):
    """Unified connector result envelope for ingest."""

    task_id: str = Field(..., description='Task identifier')
    application_id: str = Field(..., description='Application identifier')
    operation: str = Field(..., min_length=1, max_length=64, description='Operation name')
    status: str = Field(..., min_length=1, max_length=64, description='Result status')
    result_type: ResultType = Field(..., description='inline (payload) or lake_ref (location)')
    result_id: str = Field(..., description='Result identifier')
    code: str | None = Field(default=None, description='Optional status code')
    payload: dict[str, Any] | None = Field(default=None, description='Result data (required for inline)')
    location: LakeRefLocation | None = Field(default=None, description='Lake reference (required for lake_ref)')

    @model_validator(mode='after')
    def validate_result_type_requirements(self) -> 'ConnectorResultIngestRequest':
        if self.result_type == 'inline':
            if self.payload is None:
                raise ValueError('inline result_type requires payload')
        elif self.result_type == 'lake_ref':
            if self.location is None:
                raise ValueError('lake_ref result_type requires location')
        return self


class ConnectorResultIngestResponse(BaseModel):
    """Response for connector result ingest (staging row persisted)."""

    task_id: str
    result_id: str
    operation: str
    status: str

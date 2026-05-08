# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Feedback Pydantic v2 schemas."""

from __future__ import annotations

from datetime import datetime
import uuid

from pydantic import BaseModel, ConfigDict, Field
from src.inventory.assessment.feedbacks.models import FeedbackKind


class FeedbackCreate(BaseModel):
    """Payload for creating a new Feedback."""

    model_config = ConfigDict(extra='forbid')

    rule_id: int | None = None
    capability_mapping_id: int | None = None
    finding_id: int | None = None
    subject_id: uuid.UUID | None = None
    kind: FeedbackKind
    message: str = Field(min_length=1, max_length=4000)
    payload: dict | None = None
    created_by: str | None = None


class FeedbackRead(BaseModel):
    """Read schema returned from all feedback endpoints."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    rule_id: int | None
    capability_mapping_id: int | None
    finding_id: int | None
    subject_id: uuid.UUID | None
    kind: FeedbackKind
    message: str
    payload: dict | None
    created_at: datetime
    created_by: str | None

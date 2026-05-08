# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic schema for a Lens policy cartridge manifest."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from src.inventory.policy.enums import AssessmentStrategy, PolicyType


class CartridgeManifest(BaseModel):
    id: str
    version: int
    name: str
    description: str | None = None
    policy_type: PolicyType
    rule_id: str
    assessment_strategy: AssessmentStrategy
    requires: dict[str, Any] = Field(default_factory=dict)
    condition: dict[str, Any] = Field(default_factory=dict)
    decision: dict[str, Any] = Field(default_factory=dict)
    finding: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRuleCondition Pydantic v2 schemas."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from src.capabilities.access_analysis.sod_rule_conditions.models import SodRuleCondition


@dataclass
class SodRuleConditionRow:
    """Hydrated DTO returned from repository functions.

    Carries the condition ORM instance plus the resolved capability_ids list.
    Using a dataclass avoids the need for ORM relationships.
    """

    condition: SodRuleCondition
    capability_ids: list[int]


class SodRuleConditionCreate(BaseModel):
    name: str | None = None
    min_count: int = Field(default=1, ge=1)
    capability_ids: list[int] = Field(min_length=1)


class SodRuleConditionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    rule_id: int
    name: str | None
    min_count: int
    capability_ids: list[int]
    created_at: datetime

    @classmethod
    def from_row(cls, row: SodRuleConditionRow) -> SodRuleConditionRead:
        """Construct from a SodRuleConditionRow dataclass (not ORM instance directly)."""
        return cls(
            id=row.condition.id,
            rule_id=row.condition.rule_id,
            name=row.condition.name,
            min_count=row.condition.min_count,
            capability_ids=row.capability_ids,
            created_at=row.condition.created_at,
        )

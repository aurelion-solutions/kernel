# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRule Pydantic v2 schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from src.inventory.policy.sod_rules.models import SodRuleScope, SodSeverity


class SodRuleCreate(BaseModel):
    code: str
    name: str
    description: str | None = None
    severity: SodSeverity
    scope_mode: SodRuleScope
    scope_key_id: int | None = None
    is_enabled: bool = True
    mitigation_allowed: bool = True
    created_by: str | None = None


class SodRulePatch(BaseModel):
    """Partial update schema for SodRule.

    ``code`` is intentionally excluded — codes are immutable after creation.
    ``extra='forbid'`` ensures bodies containing ``code`` are rejected with 422.
    """

    model_config = ConfigDict(extra='forbid')

    name: str | None = None
    description: str | None = None
    severity: SodSeverity | None = None
    scope_mode: SodRuleScope | None = None
    scope_key_id: int | None = None
    is_enabled: bool | None = None
    mitigation_allowed: bool | None = None


class SodRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    description: str | None
    severity: SodSeverity
    scope_mode: SodRuleScope
    scope_key_id: int | None
    is_enabled: bool
    mitigation_allowed: bool
    created_at: datetime
    created_by: str | None


# ── Config-as-code apply ───────────────────────────────────────────────────────


class SodConditionSpec(BaseModel):
    """One condition inside a SodRuleSpec. Capabilities referenced by slug."""

    name: str
    min_count: int = Field(default=1, ge=1)
    capabilities: list[str] = Field(min_length=1)


class SodRuleSpec(BaseModel):
    """Declarative spec for a single SoD rule (keyed by code)."""

    code: str
    name: str
    description: str | None = None
    severity: SodSeverity
    scope_mode: SodRuleScope = SodRuleScope.global_
    mitigation_allowed: bool = True
    is_enabled: bool = True
    conditions: list[SodConditionSpec] = Field(min_length=1)


class SodApplyPayload(BaseModel):
    """Body for POST /sod-rules/apply."""

    rules: list[SodRuleSpec]
    created_by: str | None = None


class SodApplyResult(BaseModel):
    """Summary returned by POST /sod-rules/apply."""

    rules_created: int = 0
    rules_updated: int = 0
    rules_unchanged: int = 0
    conditions_created: int = 0
    conditions_deleted: int = 0
    unknown_capabilities: list[str] = Field(default_factory=list)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic v2 schemas for the Policy Decision Point (PDP) engine."""

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AbstractState(StrEnum):
    enabled = 'enabled'
    suspended = 'suspended'
    disabled = 'disabled'
    pending = 'pending'
    grace = 'grace'


class RiskLevel(StrEnum):
    critical = 'critical'
    high = 'high'
    medium = 'medium'
    low = 'low'


class Initiative(BaseModel):
    type: str
    origin: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None


class OwnerFacts(BaseModel):
    id: str
    status: str


class SubjectFacts(BaseModel):
    id: str
    kind: str
    status: str
    org_unit: str | None = None
    start_date: datetime | None = None
    term_date: datetime | None = None
    # NHI fields
    nhi_kind: str | None = None
    owner: OwnerFacts | None = None
    expires_at: datetime | None = None
    # Customer / CIAM fields
    email_verified: bool | None = None
    tenant_id: str | None = None
    tenant_role: str | None = None
    tenant_status: str | None = None
    plan_tier: str | None = None
    required_consents_met: bool = True
    mfa_enabled: bool = True


class TargetFacts(BaseModel):
    application: str
    account_status: str | None = None
    initiatives: list[Initiative] = Field(default_factory=list)
    has_pending_attestation: bool = False
    pending_reattestation: bool = False
    # Risk fields
    privilege_level: str | None = None
    environment: str | None = None
    data_sensitivity: str | None = None


class ThreatFacts(BaseModel):
    risk_score: float | None = None
    active_indicators: list[str] = Field(default_factory=list)
    days_since_last_login: int | None = None
    failed_auth_count: int | None = None


Action = str | dict[str, str]

Signal = str


class Facts(BaseModel):
    subject: SubjectFacts
    target: TargetFacts | None = None
    threat: ThreatFacts | None = None
    now: datetime


class Reason(BaseModel):
    rule_id: str
    rule_kind: str
    precedence: int
    matched_conditions: dict[str, str]
    fact_values: dict[str, Any]
    produced: dict[str, Any]


class Decision(BaseModel):
    abstract_state: AbstractState
    concrete_state: str | None = None
    risk_level: RiskLevel | None = None
    actions: list[Action] = Field(default_factory=list)
    signals: list[Signal] = Field(default_factory=list)
    reasons: list[Reason] = Field(default_factory=list)


class Rule(BaseModel):
    id: str
    kind: str
    when: dict[str, Any]
    then: dict[str, Any]
    precedence: int


class RulePack(BaseModel):
    lifecycle: list[Rule] = Field(default_factory=list)
    risk: list[Rule] = Field(default_factory=list)
    birthright: list[Rule] = Field(default_factory=list)
    mapping: dict[str, dict[str, Any]] = Field(default_factory=dict)

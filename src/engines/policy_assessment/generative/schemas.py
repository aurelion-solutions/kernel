# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Schemas for the generative PDP method.

These types are input/output contracts for GenerativePDPService.assess().
Nothing here is persisted or exposed via API directly — callers (access_plan)
map the output to their own domain types.

Origin format (C1 standard):
  policy_rule:<rule_id>
  request:<request_id>
  delegation:<delegator_subject_ref>
  grace:<source_initiative_id>
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from pydantic import BaseModel, Field
from src.engines.policy_assessment.schemas import Decision
from src.inventory.initiatives.models import InitiativeType


class SubjectContext(BaseModel):
    """Full subject snapshot supplied by the caller.

    Employee fields: org_unit_id, attributes (role, project, location,
    employment_status, etc.).
    NHI fields: application_ref, owner_subject_ref, expires_at.

    ``attributes`` carries tenant-specific key/value pairs; the PDP rules
    reference them by key (e.g. ``attributes.employment_status``).
    """

    subject_ref: str
    subject_type: str  # 'employee' | 'nhi'
    # Employee-specific
    org_unit_id: str | None = None
    # NHI-specific
    application_ref: str | None = None
    owner_subject_ref: str | None = None
    expires_at: datetime | None = None
    # Shared attributes bucket (tenant-specific key/value)
    attributes: dict[str, Any] = Field(default_factory=dict)


class CurrentInitiative(BaseModel):
    """Lightweight view of an existing initiative, passed in by the caller.

    The PDP uses this for carry-over logic (requested/delegated/grace) and
    valid_until filtering.  access_fact_id is a UUID reference to the lake
    event; the PDP treats it as an opaque identifier.
    """

    id: uuid.UUID
    access_fact_id: uuid.UUID
    type: InitiativeType
    origin: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    # Descriptor fields needed for carry-over grouping
    application: str
    target_descriptor: dict[str, Any] = Field(default_factory=dict)


class CurrentFact(BaseModel):
    """Lightweight view of an existing access fact from access_effective.

    Passed in by the caller; the PDP uses it only for carry-over grouping.
    """

    application: str
    target_descriptor: dict[str, Any] = Field(default_factory=dict)
    fact_kind: str = 'access'


class InitiativeProjection(BaseModel):
    """A projected initiative to be attached to a desired access fact.

    type: one of the 9 InitiativeType values.
    origin: standardised origin string per C1 format.
    valid_from / valid_until: carried from the source initiative (carry-over)
        or None for freshly generated birthright facts.
    source_initiative_id: set for carry-over rows; None for generated.
    """

    type: InitiativeType
    origin: str
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    source_initiative_id: uuid.UUID | None = None


class ProjectedFact(BaseModel):
    """One desired-state access fact produced by GenerativePDPService.assess().

    fact_kind: semantic kind of the fact (default 'access').
    application: target application identifier.
    target_descriptor: opaque dict describing the resource/role/group.
    initiatives: all initiative projections that justify this desired fact.
    decision: the full PDP Decision that produced this fact.
    """

    fact_kind: str
    application: str
    target_descriptor: dict[str, Any]
    initiatives: list[InitiativeProjection]
    decision: Decision

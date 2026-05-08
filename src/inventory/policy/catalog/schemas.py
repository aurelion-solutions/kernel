# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pydantic v2 schemas for the unified policy catalog endpoint.

The catalog is a product-neutral, read-only listing of every policy known to
the platform. It unifies two underlying sources:
  - DB-backed SoD policies (inventory/policy/sod_rules)
  - File-backed runnable cartridges (cartridges/lens/**/*.yaml)

Both sources are projected into the same shape via the policy axes
(policy_type, definition_source, assessment_strategy).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from src.inventory.assessment.findings.models import FindingKind
from src.inventory.policy.enums import (
    AssessmentStrategy,
    DefinitionSource,
    PolicyStatus,
    PolicyType,
)


class PolicyFindingsFilter(BaseModel):
    """Pre-built filter for `GET /api/v0/findings` so a Lens client can drill
    into the open findings produced by a single catalog policy without
    knowing the cartridge-id-to-kind mapping or the SoD `code → integer id`
    translation. Exactly one of (`kind`, `rule_id`) is set per policy:
      - file cartridges → `kind` only
      - DB SoD rules    → `rule_id` only (kind=sod is implied)
    """

    model_config = ConfigDict(frozen=True)

    kind: FindingKind | None = None
    rule_id: int | None = None


class PolicyCatalogItem(BaseModel):
    """One row in the policy catalog."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    description: str | None
    policy_type: PolicyType
    definition_source: DefinitionSource
    assessment_strategy: AssessmentStrategy
    status: PolicyStatus
    version: int | None
    open_findings_count: int
    findings_filter: PolicyFindingsFilter | None


class PolicyCatalogResponse(BaseModel):
    """Envelope for GET /api/v0/policies/catalog."""

    model_config = ConfigDict(frozen=True)

    items: list[PolicyCatalogItem]

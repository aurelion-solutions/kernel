# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Policy-domain enums shared across inventory and engine layers.

Defined here so inventory slices can use them without importing from engines.
Engine layer imports from this module (downward dependency, allowed).
"""

from __future__ import annotations

from enum import StrEnum


class PolicyType(StrEnum):
    """Policy domain axis — what kind of policy is being evaluated."""

    SOD = 'sod'
    ACCESS_RISK = 'access_risk'
    LIFECYCLE = 'lifecycle'
    NHI = 'nhi'
    PRIVILEGED_ACCESS = 'privileged_access'


class AssessmentStrategy(StrEnum):
    """Assessment strategy axis — how evidence is gathered and decisions are reached."""

    DETERMINISTIC = 'deterministic'
    HEURISTIC = 'heuristic'
    SEMANTIC_ASSISTED = 'semantic_assisted'
    HYBRID = 'hybrid'


class DefinitionSource(StrEnum):
    """Where a policy definition lives — in the database or on disk as a file."""

    DB = 'db'
    FILE = 'file'


class PolicyStatus(StrEnum):
    """Lifecycle status for a policy as it appears in the catalog.

    DB-backed SoD policies expose ``active`` (is_enabled=True) or ``inactive``.
    File-backed cartridges are inherently ``available`` — they cannot be
    toggled on or off via the catalog.
    """

    ACTIVE = 'active'
    INACTIVE = 'inactive'
    AVAILABLE = 'available'

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure Unused-Access Detector — deterministic, IO-free, DB-free.

This module is IO-free, DB-free, event-free, clock-free, and random-free.
All time data must be supplied by the caller via the ``at`` parameter.
``detect_unused`` is deterministic: given the same inputs it returns byte-identical output.

Forbidden in this module: print, logging, LogService, EventService, datetime.now, uuid.uuid4,
any asyncpg/sqlalchemy import, any DB session call, random.

"Unused" definition:
  An active AccessFact whose MAX(AccessUsageFact.last_seen) is older than threshold_days,
  OR which has no AccessUsageFact at all and whose valid_from is older than threshold_days.

Boundary arithmetic:
  Days distance uses ``(at - reference).days`` — integer floor of full elapsed days.
  Partial days do NOT round up. Example: 89.9 elapsed days → 89 → no finding at threshold 90.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from src.capabilities.access_analysis.sod_rules.models import SodSeverity

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

DEFAULT_UNUSED_SEVERITY: SodSeverity = SodSeverity.low
DEFAULT_UNUSED_THRESHOLD_DAYS: int = 90

# ---------------------------------------------------------------------------
# Input DTO (frozen, strict)
# ---------------------------------------------------------------------------


class AccessFactView(BaseModel):
    """Caller-built view of one AccessFact row joined to its Resource and usage aggregate.

    ``last_seen`` is MAX(AccessUsageFact.last_seen) for that fact; None when no usage rows exist.
    ``application_id`` is denormalized via AccessFact → Resource.application_id at SQL load time.
    ``account_id`` is nullable (system grants / subject-direct facts carry None).
    """

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: UUID
    subject_id: UUID
    account_id: UUID | None
    resource_id: UUID
    application_id: UUID
    valid_from: datetime
    last_seen: datetime | None


# ---------------------------------------------------------------------------
# Output: UnusedFinding dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnusedFinding:
    """One detected unused-access finding draft.

    ``unused_for_days`` is ``(at - last_seen).days`` when usage exists,
    or ``(at - valid_from).days`` when no usage rows exist.
    ``severity`` is always DEFAULT_UNUSED_SEVERITY (SodSeverity.low).
    ``detected_at`` is the ``at`` value passed in by the caller.
    """

    access_fact_id: UUID
    subject_id: UUID
    account_id: UUID | None
    resource_id: UUID
    application_id: UUID
    last_seen: datetime | None
    valid_from: datetime
    unused_for_days: int
    severity: SodSeverity
    detected_at: datetime


# ---------------------------------------------------------------------------
# Public detect_unused function
# ---------------------------------------------------------------------------


def detect_unused(
    *,
    access_facts: list[AccessFactView],
    threshold_days: int,
    at: datetime,
) -> list[UnusedFinding]:
    """Pure unused-access detector — deterministic, IO-free.

    For each AccessFactView:
      - If ``last_seen`` is not None: emit a finding when ``(at - last_seen).days >= threshold_days``.
      - If ``last_seen`` is None: emit a finding when ``(at - valid_from).days >= threshold_days``.

    ``unused_for_days`` in the returned finding uses the same integer-floor arithmetic as the
    threshold comparison — a single derivation shared between the boolean guard and the field value.

    Args:
        access_facts: List of AccessFactView DTOs to inspect.
        threshold_days: Minimum number of full elapsed days to qualify as unused.
                        Caller must ensure threshold_days >= 1 (validated at API boundary).
        at: Point-in-time for which detection is done (supplied by caller).

    Returns:
        Sorted list of UnusedFinding dataclasses for every fact that meets the threshold,
        deterministically ordered by ``(str(application_id), str(subject_id), str(access_fact_id))``.
        Returns an empty list when ``access_facts`` is empty or no facts meet the threshold.
    """
    findings: list[UnusedFinding] = []

    for fact in access_facts:
        if fact.last_seen is not None:
            elapsed_days = (at - fact.last_seen).days
        else:
            elapsed_days = (at - fact.valid_from).days

        if elapsed_days < threshold_days:
            continue

        findings.append(
            UnusedFinding(
                access_fact_id=fact.id,
                subject_id=fact.subject_id,
                account_id=fact.account_id,
                resource_id=fact.resource_id,
                application_id=fact.application_id,
                last_seen=fact.last_seen,
                valid_from=fact.valid_from,
                unused_for_days=elapsed_days,
                severity=DEFAULT_UNUSED_SEVERITY,
                detected_at=at,
            )
        )

    findings.sort(key=lambda f: (str(f.application_id), str(f.subject_id), str(f.access_fact_id)))
    return findings

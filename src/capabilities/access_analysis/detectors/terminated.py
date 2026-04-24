# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure Terminated-Subject Access Detector — deterministic, IO-free, DB-free.

This module is IO-free, DB-free, event-free, clock-free, and random-free.
All time data must be supplied by the caller via the ``at`` parameter.
``detect_terminated`` is deterministic: given the same inputs it returns byte-identical output.

Forbidden in this module: print, logging, LogService, EventService, datetime.now, uuid.uuid4,
any asyncpg/sqlalchemy import, any DB session call, random.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from src.capabilities.access_analysis.sod_rules.models import SodSeverity
from src.inventory.subjects.models import SubjectKind

# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------

DEFAULT_TERMINATED_SEVERITY: SodSeverity = SodSeverity.high

TERMINAL_STATUSES_BY_KIND: dict[SubjectKind, frozenset[str]] = {
    SubjectKind.employee: frozenset({'terminated'}),
    SubjectKind.nhi: frozenset({'expired', 'locked'}),
    SubjectKind.customer: frozenset({'banned', 'deletion_requested'}),
}

# ---------------------------------------------------------------------------
# Pure helper
# ---------------------------------------------------------------------------


def is_terminal_status(kind: SubjectKind, status: str) -> bool:
    """Return True if ``status`` is a terminal status for ``kind``."""
    return status in TERMINAL_STATUSES_BY_KIND.get(kind, frozenset())


# ---------------------------------------------------------------------------
# Input DTO (frozen, strict)
# ---------------------------------------------------------------------------


class AccountWithSubjectView(BaseModel):
    """Caller-built view of one Account row joined to its non-null Subject."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: UUID
    application_id: UUID
    subject_id: UUID  # non-nullable — orphan rows must never reach this DTO
    username: str
    subject_kind: SubjectKind
    subject_status: str
    subject_external_id: str


# ---------------------------------------------------------------------------
# Output: TerminatedFinding dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TerminatedFinding:
    """One detected terminated-access finding draft.

    severity is always DEFAULT_TERMINATED_SEVERITY (SodSeverity.high).
    detected_at is the ``at`` value passed in by the caller.
    """

    account_id: UUID
    application_id: UUID
    username: str
    subject_id: UUID
    subject_kind: SubjectKind
    subject_status: str
    subject_external_id: str
    severity: SodSeverity
    detected_at: datetime


# ---------------------------------------------------------------------------
# Public detect_terminated function
# ---------------------------------------------------------------------------


def detect_terminated(
    *,
    accounts: list[AccountWithSubjectView],
    at: datetime,
) -> list[TerminatedFinding]:
    """Pure terminated-subject detector — deterministic, IO-free.

    Args:
        accounts: List of AccountWithSubjectView DTOs to inspect.
        at: Point-in-time for which detection is done (supplied by caller).

    Returns:
        Sorted list of TerminatedFinding dataclasses for every account whose
        linked Subject.status is terminal for that Subject.kind, deterministically
        ordered by ``(str(application_id), username, str(account_id))``.
        Returns empty list when ``accounts`` is empty or no terminated subjects found.
    """
    findings: list[TerminatedFinding] = []

    for account in accounts:
        if not is_terminal_status(account.subject_kind, account.subject_status):
            continue
        findings.append(
            TerminatedFinding(
                account_id=account.id,
                application_id=account.application_id,
                username=account.username,
                subject_id=account.subject_id,
                subject_kind=account.subject_kind,
                subject_status=account.subject_status,
                subject_external_id=account.subject_external_id,
                severity=DEFAULT_TERMINATED_SEVERITY,
                detected_at=at,
            )
        )

    findings.sort(key=lambda f: (str(f.application_id), f.username, str(f.account_id)))
    return findings

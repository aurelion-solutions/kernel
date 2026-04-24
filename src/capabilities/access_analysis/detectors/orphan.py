# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure Orphan Access Detector — deterministic, IO-free, DB-free.

This module is IO-free, DB-free, event-free, clock-free, and random-free.
All time data must be supplied by the caller via the ``at`` parameter.
``detect_orphans`` is deterministic: given the same inputs it returns byte-identical output.

Forbidden in this module: print, logging, LogService, datetime.now, uuid.uuid4,
any asyncpg/sqlalchemy import, any DB session call, random.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from src.capabilities.access_analysis.sod_rules.models import SodSeverity

# ---------------------------------------------------------------------------
# Module constant
# ---------------------------------------------------------------------------

DEFAULT_ORPHAN_SEVERITY: SodSeverity = SodSeverity.high

# ---------------------------------------------------------------------------
# Input DTO (frozen, strict)
# ---------------------------------------------------------------------------


class AccountView(BaseModel):
    """Caller-built view of one Account row with last known owner surfaced."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: UUID
    application_id: UUID
    subject_id: UUID | None
    username: str
    last_known_owner_subject_id: UUID | None


# ---------------------------------------------------------------------------
# Output: OrphanFinding dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrphanFinding:
    """One detected orphan-access finding draft.

    severity is always DEFAULT_ORPHAN_SEVERITY (SodSeverity.high).
    detected_at is the ``at`` value passed in by the caller.
    """

    account_id: UUID
    application_id: UUID
    username: str
    severity: SodSeverity
    last_known_owner_subject_id: UUID | None
    detected_at: datetime


# ---------------------------------------------------------------------------
# Public detect_orphans function
# ---------------------------------------------------------------------------


def detect_orphans(
    *,
    accounts: list[AccountView],
    at: datetime,
) -> list[OrphanFinding]:
    """Pure orphan detector — deterministic, IO-free.

    Args:
        accounts: List of AccountView DTOs to inspect.
        at: Point-in-time for which detection is done (supplied by caller).

    Returns:
        Sorted list of OrphanFinding dataclasses for every account where
        ``subject_id IS NULL``, deterministically ordered by
        ``(str(application_id), username, str(account_id))``.
        Returns empty list when ``accounts`` is empty or no orphans found.
    """
    findings: list[OrphanFinding] = []

    for account in accounts:
        if account.subject_id is not None:
            continue
        findings.append(
            OrphanFinding(
                account_id=account.id,
                application_id=account.application_id,
                username=account.username,
                severity=DEFAULT_ORPHAN_SEVERITY,
                last_known_owner_subject_id=account.last_known_owner_subject_id,
                detected_at=at,
            )
        )

    findings.sort(key=lambda f: (str(f.application_id), f.username, str(f.account_id)))
    return findings

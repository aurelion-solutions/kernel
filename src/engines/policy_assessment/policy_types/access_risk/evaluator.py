# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Access-risk policy type — pure evaluators, IO-free, DB-free.

Two rule/cartridge functions live here:
  detect_orphans   — orphaned_access rule: accounts with no linked subject.
  detect_unused    — unused_access rule: access not exercised within threshold.

Both are deterministic: given the same inputs they return byte-identical output.

Forbidden in this module: print, logging, LogService, datetime.now, uuid.uuid4,
any asyncpg/sqlalchemy import, any DB session call, random.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from src.inventory.policy.sod_rules.models import SodSeverity

# ---------------------------------------------------------------------------
# orphaned_access rule
# ---------------------------------------------------------------------------

DEFAULT_ORPHAN_SEVERITY: SodSeverity = SodSeverity.high


class AccountView(BaseModel):
    """Caller-built view of one Account row with last known owner surfaced."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: UUID
    application_id: UUID
    subject_id: UUID | None
    username: str
    last_known_owner_subject_id: UUID | None


@dataclass(frozen=True)
class OrphanFinding:
    """One detected orphaned-access finding draft."""

    account_id: UUID
    application_id: UUID
    username: str
    severity: SodSeverity
    last_known_owner_subject_id: UUID | None
    detected_at: datetime


def detect_orphans(
    *,
    accounts: list[AccountView],
    at: datetime,
) -> list[OrphanFinding]:
    """Pure orphaned-access detector — deterministic, IO-free.

    Returns sorted list of OrphanFinding for every account where subject_id IS NULL.
    Sort key: (str(application_id), username, str(account_id)).
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


# ---------------------------------------------------------------------------
# unused_access rule
# ---------------------------------------------------------------------------

DEFAULT_UNUSED_SEVERITY: SodSeverity = SodSeverity.low
DEFAULT_UNUSED_THRESHOLD_DAYS: int = 90


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


@dataclass(frozen=True)
class UnusedFinding:
    """One detected unused-access finding draft.

    ``unused_for_days`` is ``(at - last_seen).days`` when usage exists,
    or ``(at - valid_from).days`` when no usage rows exist.
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


def detect_unused(
    *,
    access_facts: list[AccessFactView],
    threshold_days: int,
    at: datetime,
) -> list[UnusedFinding]:
    """Pure unused-access detector — deterministic, IO-free.

    Emits a finding when elapsed days since last_seen (or valid_from) >= threshold_days.
    Days arithmetic uses integer floor of full elapsed days.
    Sort key: (str(application_id), str(subject_id), str(access_fact_id)).
    """
    findings: list[UnusedFinding] = []
    for fact in access_facts:
        elapsed_days = (at - fact.last_seen).days if fact.last_seen is not None else (at - fact.valid_from).days
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


# ---------------------------------------------------------------------------
# privileged_access rule
# ---------------------------------------------------------------------------

DEFAULT_PRIVILEGED_SEVERITY: SodSeverity = SodSeverity.high


class PrivilegedCandidateView(BaseModel):
    """Caller-built view of one active EffectiveGrant joined to its Account and Resource.

    Carries only the deterministic privileged-signal fields the cartridge consumes:
    account-level ``is_privileged``, the action verb, and resource privilege /
    environment / data sensitivity. ``account_id`` is nullable for grants that
    are not yet linked to a remote account.
    """

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    effective_grant_id: UUID
    subject_id: UUID
    account_id: UUID | None
    application_id: UUID
    resource_id: UUID
    action: str
    account_is_privileged: bool
    resource_privilege_level: str | None
    resource_environment: str | None
    resource_data_sensitivity: str | None

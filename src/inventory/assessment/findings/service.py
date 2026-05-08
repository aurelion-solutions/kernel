# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Finding service — business logic for the Finding slice.

No events and no logs are emitted by this service — finding.* events are
emitted by the engine (Step 14) and the mitigation flow (Step 9).
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.assessment.findings.exceptions import (
    FindingMissingReasonError,
    FindingMitigationLinkageMissingError,
    FindingMitigationNotApplicableError,
    FindingNotFoundError,
    FindingStatusTransitionError,
)
from src.inventory.assessment.findings.models import FindingKind, FindingStatus
from src.inventory.assessment.findings.repository import (
    get_finding_by_id,
    get_mitigation_for_linkage,
    list_findings,
    update_finding_status_fields,
)
from src.inventory.assessment.findings.schemas import FindingRead, FindingStatusPatch
from src.inventory.assessment.mitigations.models import Mitigation, MitigationStatus
from src.inventory.policy.sod_rules.models import SodSeverity
from src.platform.logs.service import LogService

# Allowed status transitions set.
# Terminal states: resolved (operator-override terminal), mitigated.
# Any non-terminal → resolved is allowed (operator override).
# Explicit allowed non-resolved transitions:
#   open → acknowledged
#   open → mitigated
#   acknowledged → mitigated
_ALLOWED_TRANSITIONS: frozenset[tuple[FindingStatus, FindingStatus]] = frozenset(
    [
        (FindingStatus.open, FindingStatus.acknowledged),
        (FindingStatus.open, FindingStatus.mitigated),
        (FindingStatus.acknowledged, FindingStatus.mitigated),
        # any non-terminal → resolved (handled separately in the validator)
    ]
)

_TERMINAL_STATUSES: frozenset[FindingStatus] = frozenset([FindingStatus.resolved, FindingStatus.mitigated])


def _validate_status_transition(
    from_status: FindingStatus,
    to_status: FindingStatus,
    status_reason: str | None,
) -> None:
    """Raise FindingStatusTransitionError or FindingMissingReasonError if invalid.

    Rules:
    - resolved is terminal (no transition out)
    - mitigated is terminal (no transition out)
    - open → acknowledged: allowed
    - open → mitigated: allowed
    - acknowledged → mitigated: allowed
    - any non-terminal → resolved: allowed (requires status_reason)
    - same-state and all other transitions: rejected
    """
    # Same-state → always rejected
    if from_status == to_status:
        raise FindingStatusTransitionError(from_status, to_status)

    # Out of terminal → always rejected
    if from_status in _TERMINAL_STATUSES:
        raise FindingStatusTransitionError(from_status, to_status)

    # Any non-terminal → resolved: allowed but requires reason
    if to_status == FindingStatus.resolved:
        if not status_reason:
            raise FindingMissingReasonError()
        return

    # Check against explicit allowed set
    if (from_status, to_status) not in _ALLOWED_TRANSITIONS:
        raise FindingStatusTransitionError(from_status, to_status)


def _validate_mitigation_linkage(
    mitigation: Mitigation | None,
    mitigation_id: int,
    finding_rule_id: int | None,
    finding_subject_id: uuid.UUID | None,
    finding_scope_key_id: int | None,
    finding_scope_value: str | None,
    now: datetime,
) -> None:
    """Validate that the referenced Mitigation is eligible for linkage with a Finding.

    Rules (per TASK.md §3B):
    1. Mitigation must exist.
    2. Mitigation.status must be 'active'.
    3. valid_from <= now <= valid_until (or valid_until IS NULL).
    4. (rule_id, subject_id) must match the finding.
    5. Scope: exact match OR unscoped mitigation (scope_key_id IS NULL).

    Raises FindingMitigationNotApplicableError with a descriptive reason string.
    """
    if mitigation is None:
        raise FindingMitigationNotApplicableError(mitigation_id, 'not found')

    if mitigation.status != MitigationStatus.active:
        raise FindingMitigationNotApplicableError(mitigation_id, 'not active')

    if mitigation.valid_from > now or (mitigation.valid_until is not None and mitigation.valid_until <= now):
        raise FindingMitigationNotApplicableError(mitigation_id, 'expired window')

    if mitigation.rule_id != finding_rule_id or mitigation.subject_id != finding_subject_id:
        raise FindingMitigationNotApplicableError(mitigation_id, 'rule/subject mismatch')

    # Specific-overrides-generic scope check
    exact_match = mitigation.scope_key_id == finding_scope_key_id and mitigation.scope_value == finding_scope_value
    unscoped = mitigation.scope_key_id is None  # scope_value IS NULL guaranteed by DB CHECK
    if not exact_match and not unscoped:
        raise FindingMitigationNotApplicableError(mitigation_id, 'scope mismatch')


class FindingService:
    """Read + status-transition service for the Finding slice.

    ``log_service`` is plumbed for parity with other slices but is not used in
    this step — event emission is the engine's responsibility (Step 14).
    """

    def __init__(self, session: AsyncSession, log_service: LogService) -> None:
        self._session = session
        self._log_service = log_service

    async def list(
        self,
        *,
        scan_run_id: int | None = None,
        rule_id: int | None = None,
        severity: SodSeverity | None = None,
        status: FindingStatus | None = None,
        kind: FindingKind | None = None,
        subject_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FindingRead]:
        """Return Findings, optionally filtered."""
        rows = await list_findings(
            self._session,
            scan_run_id=scan_run_id,
            rule_id=rule_id,
            severity=severity,
            status=status,
            kind=kind,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
        return [FindingRead.model_validate(row) for row in rows]

    async def get(self, finding_id: int) -> FindingRead:
        """Return a Finding by id. Raises FindingNotFoundError when missing."""
        finding = await get_finding_by_id(self._session, finding_id)
        if finding is None:
            raise FindingNotFoundError(finding_id)
        return FindingRead.model_validate(finding)

    async def patch_status(self, finding_id: int, payload: FindingStatusPatch) -> FindingRead:
        """Transition a Finding's status.

        Raises FindingNotFoundError if the finding does not exist.
        Raises FindingStatusTransitionError for illegal transitions.
        Raises FindingMissingReasonError when transitioning to 'resolved' without a reason.
        Raises FindingMitigationLinkageMissingError when transitioning to 'mitigated'
            without a usable active_mitigation_id.
        Raises FindingMitigationNotApplicableError when the referenced mitigation
            fails linkage validation.
        """
        finding = await get_finding_by_id(self._session, finding_id)
        if finding is None:
            raise FindingNotFoundError(finding_id)

        _validate_status_transition(finding.status, payload.status, payload.status_reason)

        now = datetime.now(tz=UTC)
        resolved_mitigation_id: int | None = None

        if payload.status == FindingStatus.mitigated:
            mit_id = payload.active_mitigation_id or finding.active_mitigation_id
            if mit_id is None:
                raise FindingMitigationLinkageMissingError()
            mitigation = await get_mitigation_for_linkage(self._session, mit_id)
            _validate_mitigation_linkage(
                mitigation,
                mitigation_id=mit_id,
                finding_rule_id=finding.rule_id,
                finding_subject_id=finding.subject_id,
                finding_scope_key_id=finding.scope_key_id,
                finding_scope_value=finding.scope_value,
                now=now,
            )
            resolved_mitigation_id = mit_id

        finding = await update_finding_status_fields(
            self._session,
            finding,
            status=payload.status,
            status_changed_at=now,
            status_reason=payload.status_reason,
            active_mitigation_id=resolved_mitigation_id,
        )
        return FindingRead.model_validate(finding)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ScanEngine — batch orchestrator for finding detection and persistence.

Architecture principles enforced here:
- The engine is stateless across runs; one instance is reusable.
- Per-run state lives in method locals only.
- The engine never emits events directly — it returns EngineResult to service.py,
  which is the sole emitter (per ARCH_CONTEXT: "Only services emit events").
- The engine never calls session.commit() — service flushes, route handler commits.
- Bulk loaders use a single round-trip per producer per scan run (no N+1).
- Capability grants are filtered by the active-at predicate before being passed to
  the evaluator. The evaluator does not re-filter.
- Mitigations are filtered by validity window before being passed to the evaluator.
- orphaned_access, terminated_subject_access, unused_access, and privileged_access
  are all evaluated via their respective lens/ cartridges through
  PolicyCartridgeAssessmentService.
- severity for cartridge-backed findings is sourced from cartridge
  decision.risk_level (Phase 34); the per-policy DEFAULT_*_SEVERITY constants
  (orphaned_access=high, terminated_subject_access=high, unused_access=low,
  privileged_access=high) act as fallback only when the cartridge response
  carries no Decision.

Concurrency note: under concurrent scans the dedup pre-SELECT can race with
another writer's insert. The dedupe helper catches IntegrityError and re-SELECTs.
POST /scan-runs/{id}/run is synchronous and serial per run — races only occur
across overlapping runs of different ScanRun rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_analysis._engine_dedupe import (
    FindingEmission,
    StatusChangeEmission,
    upsert_finding,
)
from src.engines.access_analysis._engine_loaders import (
    load_all_mitigations,
    load_all_subject_ids,
    load_capability_grants_for_subject,
    load_orphan_inputs,
    load_privileged_inputs,
    load_sod_rules,
    load_terminated_inputs,
    load_unused_inputs,
)
from src.engines.policy_assessment.cartridge_service import PolicyCartridgeAssessmentService
from src.engines.policy_assessment.contracts import PolicyAssessmentOutput
from src.engines.policy_assessment.policy_types.access_risk.evaluator import (
    DEFAULT_ORPHAN_SEVERITY,
    DEFAULT_PRIVILEGED_SEVERITY,
    DEFAULT_UNUSED_SEVERITY,
)
from src.engines.policy_assessment.policy_types.lifecycle.evaluator import (
    DEFAULT_TERMINATED_SEVERITY,
)
from src.engines.policy_assessment.policy_types.sod.evaluator import MitigationView, evaluate
from src.inventory.assessment.findings.models import FindingKind
from src.inventory.assessment.scan_runs.models import ScanRun
from src.inventory.policy.sod_rules.models import SodSeverity
from src.platform.lake.duckdb_session import LakeSession
from src.platform.logs.service import LogService

# ---------------------------------------------------------------------------
# EngineResult contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngineFailure:
    """Carries failure details when the engine aborts mid-run."""

    error_class: str
    error_message: str


@dataclass(frozen=True)
class EngineResult:
    """Structured return value from ScanEngine.run(). Service uses this to update ScanRun and emit events."""

    findings_created: list[FindingEmission]
    findings_reused: list[FindingEmission]
    findings_status_changed: list[StatusChangeEmission]
    findings_by_severity: dict[str, int]
    findings_total: int
    error: EngineFailure | None = None


_ORPHANED_ACCESS_CARTRIDGE: Path = (
    Path(__file__).resolve().parents[4] / 'cartridges' / 'lens' / 'access_risk' / 'orphaned_access.yaml'
)

_TERMINATED_ACCESS_CARTRIDGE: Path = (
    Path(__file__).resolve().parents[4] / 'cartridges' / 'lens' / 'lifecycle' / 'terminated_subject_access.yaml'
)

_UNUSED_ACCESS_CARTRIDGE: Path = (
    Path(__file__).resolve().parents[4] / 'cartridges' / 'lens' / 'access_risk' / 'unused_access.yaml'
)

_PRIVILEGED_ACCESS_CARTRIDGE: Path = (
    Path(__file__).resolve().parents[4] / 'cartridges' / 'lens' / 'access_risk' / 'privileged_access.yaml'
)


# ---------------------------------------------------------------------------
# Evidence hash helpers for non-SoD findings
# ---------------------------------------------------------------------------


def _orphan_evidence_hash(account_id: UUID) -> str:
    """Stable evidence hash for orphan_access findings (account-anchored)."""
    payload = json.dumps({'account_id': str(account_id), 'kind': 'orphan_access'}, sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _terminated_evidence_hash(account_id: UUID, subject_id: UUID) -> str:
    """Stable evidence hash for terminated_access findings."""
    payload = json.dumps(
        {'account_id': str(account_id), 'kind': 'terminated_access', 'subject_id': str(subject_id)},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _unused_evidence_hash(access_fact_id: UUID) -> str:
    """Stable evidence hash for unused_access findings (access-fact-anchored)."""
    payload = json.dumps({'access_fact_id': str(access_fact_id), 'kind': 'unused_access'}, sort_keys=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _privileged_evidence_hash(effective_grant_id: UUID) -> str:
    """Stable evidence hash for privileged_access findings (effective-grant-anchored)."""
    payload = json.dumps(
        {'effective_grant_id': str(effective_grant_id), 'kind': 'privileged_access'},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# Severity rollup helper
# ---------------------------------------------------------------------------


def _rollup_severity(
    severity: SodSeverity,
    acc: dict[str, int],
) -> dict[str, int]:
    """Increment severity count in accumulator. Returns modified copy."""
    updated = dict(acc)
    key = severity.value
    updated[key] = updated.get(key, 0) + 1
    return updated


def _severity_from_output(output: PolicyAssessmentOutput, default: SodSeverity) -> SodSeverity:
    """Cartridge-backed severity resolution.

    The cartridge YAML is the source of truth for cartridge-backed findings.
    Falls back to the engine's default only when the cartridge does not produce
    a Decision with a risk_level (e.g. tests using bare PolicyAssessmentOutput
    mocks without decision).
    """
    if output.decision is not None and output.decision.risk_level is not None:
        return SodSeverity(output.decision.risk_level.value)
    return default


# ---------------------------------------------------------------------------
# ScanEngine
# ---------------------------------------------------------------------------


class ScanEngine:
    """Stateless batch orchestrator.

    One instance can be reused across scan runs. Per-run state lives in locals only.
    """

    def __init__(
        self,
        cartridge_service: PolicyCartridgeAssessmentService | None = None,
    ) -> None:
        self._cartridge_service = (
            cartridge_service if cartridge_service is not None else PolicyCartridgeAssessmentService()
        )

    async def run(
        self,
        session: AsyncSession,
        scan_run: ScanRun,
        *,
        at: datetime,
        correlation_id: str,
        lake_session: LakeSession,
        log_service: LogService,
        pg_any_array_max_size: int,
    ) -> EngineResult:
        """Execute one scan run end-to-end.

        Steps:
          1. Load SoD rules + all mitigations (once per run).
          2. For each subject with CapabilityGrants: evaluate SoD, upsert findings.
          3. Load orphan accounts → detect → upsert findings.
          4. Load terminated accounts → detect → upsert findings.
          5. Load unused access facts → detect → upsert findings.
          6. Load privileged-access candidates → detect → upsert findings.
          6. Compute severity rollup over all findings (created + reused).
          7. Return EngineResult.

        Raises any DB error — callers (service.py) wrap in try/except to set status=failed.
        Never emits events. Never commits.
        """
        scope_subject_id: UUID | None = scan_run.scope_subject_id
        scope_application_id: UUID | None = scan_run.scope_application_id

        findings_created: list[FindingEmission] = []
        findings_reused: list[FindingEmission] = []
        findings_status_changed: list[StatusChangeEmission] = []
        findings_by_severity: dict[str, int] = {}

        try:
            # ----------------------------------------------------------------
            # 1. SoD: load rules + all mitigations once
            # ----------------------------------------------------------------
            rules = await load_sod_rules(session)
            all_mitigations = await load_all_mitigations(session, at, scope_subject_id=scope_subject_id)
            # Build per-subject mitigation lookup
            mitigations_by_subject: dict[UUID, list[MitigationView]] = {}
            for m in all_mitigations:
                if m.subject_id not in mitigations_by_subject:
                    mitigations_by_subject[m.subject_id] = []
                mitigations_by_subject[m.subject_id].append(m)

            # ----------------------------------------------------------------
            # 2. SoD: per-subject evaluation
            # ----------------------------------------------------------------
            subject_ids = await load_all_subject_ids(session, scope_subject_id=scope_subject_id)

            for subject_id in subject_ids:
                grants = await load_capability_grants_for_subject(session, subject_id, at)
                subject_mitigations = mitigations_by_subject.get(subject_id, [])
                violations = evaluate(
                    subject_id=subject_id,
                    capability_grants=grants,
                    rules=rules,
                    mitigations=subject_mitigations,
                    at=at,
                )

                for v in violations:
                    emission, is_new, sc = await upsert_finding(
                        session,
                        scan_run_id=scan_run.id,
                        kind=FindingKind.sod,
                        subject_id=subject_id,
                        account_id=None,
                        rule_id=v.rule_id,
                        scope_key_id=v.scope_key_id,
                        scope_value=v.scope_value,
                        evidence_hash=v.evidence_hash,
                        severity=v.severity,
                        evaluated_at=at,
                        matched_capability_grant_ids=v.matched_capability_grant_ids,
                        matched_effective_grant_ids=[str(eid) for eid in v.matched_effective_grant_ids],
                        matched_access_fact_ids=[],
                        active_mitigation_id=v.active_mitigation_id,
                        proposed_mitigation_id=v.proposed_mitigation_id,
                    )
                    if is_new:
                        findings_created.append(emission)
                    else:
                        findings_reused.append(emission)
                    if sc is not None:
                        findings_status_changed.append(sc)
                    findings_by_severity = _rollup_severity(v.severity, findings_by_severity)

            # ----------------------------------------------------------------
            # 3. Orphan detector — evaluated via lens.access_risk.orphaned_access cartridge
            # ----------------------------------------------------------------
            orphan_accounts = await load_orphan_inputs(session, scope_application_id=scope_application_id)

            for account in orphan_accounts:
                ctx = {'subject_not_found': account.subject_id is None}
                output = self._cartridge_service.evaluate_file(_ORPHANED_ACCESS_CARTRIDGE, ctx)
                if not output.matched:
                    continue

                severity = _severity_from_output(output, DEFAULT_ORPHAN_SEVERITY)
                evidence_hash = _orphan_evidence_hash(account.id)
                emission, is_new, sc = await upsert_finding(
                    session,
                    scan_run_id=scan_run.id,
                    kind=FindingKind.orphan_access,
                    subject_id=None,
                    account_id=account.id,
                    rule_id=None,
                    scope_key_id=None,
                    scope_value=None,
                    evidence_hash=evidence_hash,
                    severity=severity,
                    evaluated_at=at,
                    matched_capability_grant_ids=[],
                    matched_effective_grant_ids=[],
                    matched_access_fact_ids=[],
                    active_mitigation_id=None,
                    proposed_mitigation_id=None,
                )
                if is_new:
                    findings_created.append(emission)
                else:
                    findings_reused.append(emission)
                if sc is not None:
                    findings_status_changed.append(sc)
                findings_by_severity = _rollup_severity(severity, findings_by_severity)

            # ----------------------------------------------------------------
            # 4. Terminated detector — evaluated via lens.lifecycle.terminated_subject_access cartridge
            # ----------------------------------------------------------------
            terminated_accounts = await load_terminated_inputs(
                session,
                scope_subject_id=scope_subject_id,
                scope_application_id=scope_application_id,
            )

            for account in terminated_accounts:
                ctx = {'subject': {'status': account.subject_status}}
                output = self._cartridge_service.evaluate_file(_TERMINATED_ACCESS_CARTRIDGE, ctx)
                if not output.matched:
                    continue

                severity = _severity_from_output(output, DEFAULT_TERMINATED_SEVERITY)
                evidence_hash = _terminated_evidence_hash(account.id, account.subject_id)
                emission, is_new, sc = await upsert_finding(
                    session,
                    scan_run_id=scan_run.id,
                    kind=FindingKind.terminated_access,
                    subject_id=account.subject_id,
                    account_id=account.id,
                    rule_id=None,
                    scope_key_id=None,
                    scope_value=None,
                    evidence_hash=evidence_hash,
                    severity=severity,
                    evaluated_at=at,
                    matched_capability_grant_ids=[],
                    matched_effective_grant_ids=[],
                    matched_access_fact_ids=[],
                    active_mitigation_id=None,
                    proposed_mitigation_id=None,
                )
                if is_new:
                    findings_created.append(emission)
                else:
                    findings_reused.append(emission)
                if sc is not None:
                    findings_status_changed.append(sc)
                findings_by_severity = _rollup_severity(severity, findings_by_severity)

            # ----------------------------------------------------------------
            # 5. Unused detector — evaluated via lens.access_risk.unused_access cartridge
            # ----------------------------------------------------------------
            unused_facts = await load_unused_inputs(
                session,
                lake_session,
                log_service,
                scope_subject_id=scope_subject_id,
                scope_application_id=scope_application_id,
                batch_size=1000,
                pg_any_array_max_size=pg_any_array_max_size,
            )

            for fact in unused_facts:
                last_active = fact.last_seen if fact.last_seen is not None else fact.valid_from
                days_since_last_use = (at - last_active).days
                ctx = {'days_since_last_use': days_since_last_use}
                output = self._cartridge_service.evaluate_file(_UNUSED_ACCESS_CARTRIDGE, ctx)
                if not output.matched:
                    continue

                severity = _severity_from_output(output, DEFAULT_UNUSED_SEVERITY)
                evidence_hash = _unused_evidence_hash(fact.id)
                emission, is_new, sc = await upsert_finding(
                    session,
                    scan_run_id=scan_run.id,
                    kind=FindingKind.unused_access,
                    subject_id=fact.subject_id,
                    account_id=fact.account_id,
                    rule_id=None,
                    scope_key_id=None,
                    scope_value=None,
                    evidence_hash=evidence_hash,
                    severity=severity,
                    evaluated_at=at,
                    matched_capability_grant_ids=[],
                    matched_effective_grant_ids=[],
                    matched_access_fact_ids=[str(fact.id)],
                    active_mitigation_id=None,
                    proposed_mitigation_id=None,
                )
                if is_new:
                    findings_created.append(emission)
                else:
                    findings_reused.append(emission)
                if sc is not None:
                    findings_status_changed.append(sc)
                findings_by_severity = _rollup_severity(severity, findings_by_severity)

            # ----------------------------------------------------------------
            # 6. Privileged-access detector — evaluated via lens.access_risk.privileged_access cartridge
            # ----------------------------------------------------------------
            privileged_candidates = await load_privileged_inputs(
                session,
                scope_subject_id=scope_subject_id,
                scope_application_id=scope_application_id,
            )

            for candidate in privileged_candidates:
                ctx = {
                    'account_is_privileged': candidate.account_is_privileged,
                    'action': candidate.action,
                    'resource_privilege_level': candidate.resource_privilege_level,
                    'environment': candidate.resource_environment,
                    'data_sensitivity': candidate.resource_data_sensitivity,
                }
                output = self._cartridge_service.evaluate_file(_PRIVILEGED_ACCESS_CARTRIDGE, ctx)
                if not output.matched:
                    continue

                severity = _severity_from_output(output, DEFAULT_PRIVILEGED_SEVERITY)
                evidence_hash = _privileged_evidence_hash(candidate.effective_grant_id)
                emission, is_new, sc = await upsert_finding(
                    session,
                    scan_run_id=scan_run.id,
                    kind=FindingKind.privileged_access,
                    subject_id=candidate.subject_id,
                    account_id=candidate.account_id,
                    rule_id=None,
                    scope_key_id=None,
                    scope_value=None,
                    evidence_hash=evidence_hash,
                    severity=severity,
                    evaluated_at=at,
                    matched_capability_grant_ids=[],
                    matched_effective_grant_ids=[str(candidate.effective_grant_id)],
                    matched_access_fact_ids=[],
                    active_mitigation_id=None,
                    proposed_mitigation_id=None,
                )
                if is_new:
                    findings_created.append(emission)
                else:
                    findings_reused.append(emission)
                if sc is not None:
                    findings_status_changed.append(sc)
                findings_by_severity = _rollup_severity(severity, findings_by_severity)

        except Exception as exc:
            return EngineResult(
                findings_created=[],
                findings_reused=[],
                findings_status_changed=[],
                findings_by_severity={},
                findings_total=0,
                error=EngineFailure(
                    error_class=type(exc).__name__,
                    error_message=str(exc),
                ),
            )

        return EngineResult(
            findings_created=findings_created,
            findings_reused=findings_reused,
            findings_status_changed=findings_status_changed,
            findings_by_severity=findings_by_severity,
            findings_total=len(findings_created) + len(findings_reused),
        )

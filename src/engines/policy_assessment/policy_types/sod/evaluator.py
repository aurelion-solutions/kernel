# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Pure SoD Evaluator — deterministic, IO-free, DB-free.

This module is IO-free, DB-free, event-free, clock-free, and random-free.
All time data must be supplied by the caller via the ``at`` parameter.
``evaluate`` is deterministic: given the same inputs it returns byte-identical output.

Evidence hash contract (frozen from this step onward):
- SHA-256 hex digest over canonical JSON of:
  subject_id (str), sorted unique access_fact_id ints, sorted unique initiative_id ints,
  sorted unique capability_mapping_id ints, rule_id (int), scope_key_id (int|null),
  scope_value (str|null). Never includes EffectiveGrant.id — that is regenerated on EAS rebuild.
- Changing any of these inputs (adding a new field, changing field order) would invalidate every
  persisted Finding.evidence_hash once Step 14 ships. The encoder is a frozen contract.

Mitigation resolution (specific-overrides-generic):
- Tier 1 (exact): (rule_id, subject_id, scope_key_id, scope_value) match
- Tier 2 (generic): (rule_id, subject_id, scope_key_id=None, scope_value=None)
- Within a tier: active > proposed; then most recent created_at
- Only active status flips is_mitigated=True; proposed populates proposed_mitigation_id only.

What-if: capability_overrides are unioned with capability_grants before bucketing.
Overrides without application_id cannot satisfy PER_APPLICATION rules; without scope_value
cannot satisfy BY_SCOPE_KEY rules. Evaluator does not reject under-specified overrides.

Forbidden in this module: print, logging, LogService, datetime.now, uuid.uuid4,
any asyncpg/sqlalchemy import, any DB session call, random.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from src.inventory.assessment.mitigations.models import MitigationStatus
from src.inventory.policy.sod_rules.models import SodRuleScope, SodSeverity

# ---------------------------------------------------------------------------
# Input DTOs (frozen, strict)
# ---------------------------------------------------------------------------


class CapabilityGrantView(BaseModel):
    """Caller-built view of one CapabilityGrant row with denormalized fields."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: int
    subject_id: UUID
    capability_id: int
    capability_slug: str
    scope_key_id: int
    scope_value: str | None
    application_id: UUID
    source_effective_grant_id: UUID
    source_capability_mapping_id: int
    source_access_fact_ids: list[int]
    source_initiative_ids: list[int]


class SodRuleConditionView(BaseModel):
    """Caller-built view of one SodRuleCondition row with M2M capability_ids preloaded."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: int
    name: str | None
    min_count: int
    capability_ids: frozenset[int]


class SodRuleView(BaseModel):
    """Caller-built view of one SodRule row with conditions preloaded."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: int
    code: str
    severity: SodSeverity
    scope_mode: SodRuleScope
    scope_key_id: int | None
    is_enabled: bool
    conditions: tuple[SodRuleConditionView, ...]


class MitigationView(BaseModel):
    """Caller-built view of one Mitigation row (active or proposed, window-filtered by caller)."""

    model_config = ConfigDict(frozen=True, extra='forbid', strict=True)

    id: int
    rule_id: int
    subject_id: UUID
    scope_key_id: int | None
    scope_value: str | None
    status: MitigationStatus
    valid_from: datetime
    valid_until: datetime | None
    created_at: datetime


# ---------------------------------------------------------------------------
# Output: Violation dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One detected SoD violation.

    matched_effective_grant_ids: list of EffectiveGrant.id (UUID) from matched grants.
    Serialized as strings in SodViolationResponse for JSON-friendly output.
    """

    rule_id: int
    rule_code: str
    severity: SodSeverity
    scope_mode: SodRuleScope
    scope_key_id: int | None
    scope_value: str | None
    matched_condition_ids: list[int]
    matched_capability_slugs: list[str]
    matched_capability_grant_ids: list[int]
    matched_effective_grant_ids: list[UUID]
    evidence_hash: str
    is_mitigated: bool
    active_mitigation_id: int | None
    proposed_mitigation_id: int | None
    evaluated_at: datetime


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _bucket_grants(
    grants: list[CapabilityGrantView],
    rule: SodRuleView,
) -> dict[object, list[CapabilityGrantView]]:
    """Group grants into scope buckets per rule.scope_mode.

    Returns:
      GLOBAL        → {None: [all grants]}
      PER_APPLICATION → {app_id: [grants for that app], ...}
      BY_SCOPE_KEY  → {scope_value: [grants with rule.scope_key_id and that value], ...}

    Empty inputs → empty dict (no synthetic buckets).
    """
    if rule.scope_mode == SodRuleScope.global_:
        if not grants:
            return {}
        return {None: list(grants)}

    if rule.scope_mode == SodRuleScope.per_application:
        buckets: dict[object, list[CapabilityGrantView]] = {}
        for g in grants:
            app_id = g.application_id
            if app_id not in buckets:
                buckets[app_id] = []
            buckets[app_id].append(g)
        return buckets

    # BY_SCOPE_KEY
    if rule.scope_mode == SodRuleScope.by_scope_key:
        buckets = {}
        for g in grants:
            if g.scope_key_id != rule.scope_key_id:
                continue
            sv = g.scope_value
            if sv is None:
                # null scope_value cannot satisfy BY_SCOPE_KEY (GLOBAL sentinel)
                continue
            if sv not in buckets:
                buckets[sv] = []
            buckets[sv].append(g)
        return buckets

    # Exhaustive — unreachable if SodRuleScope is complete
    raise ValueError(f'Unknown scope_mode: {rule.scope_mode!r}')  # pragma: no cover


def _intersect_distinct_slugs(
    bucket: list[CapabilityGrantView],
    condition: SodRuleConditionView,
) -> frozenset[str]:
    """Return distinct capability slugs in bucket that intersect condition.capability_ids.

    Multiple CapabilityGrant rows for the same slug count as ONE (distinct slugs, not row count).
    """
    seen: set[str] = set()
    for g in bucket:
        if g.capability_id in condition.capability_ids:
            seen.add(g.capability_slug)
    return frozenset(seen)


def _resolve_mitigation(
    rule_id: int,
    subject_id: UUID,
    scope_key_id: int | None,
    scope_value: str | None,
    mitigations: list[MitigationView],
) -> tuple[int | None, int | None]:
    """Resolve mitigation for a candidate violation.

    Returns (active_mitigation_id, proposed_mitigation_id).

    Specific-overrides-generic, then active > proposed, then most recent created_at.
    The caller is responsible for filtering mitigations to those whose validity
    window covers the evaluation timestamp.
    """
    # Filter to mitigations for this rule and subject
    relevant = [
        m
        for m in mitigations
        if m.rule_id == rule_id
        and m.subject_id == subject_id
        and m.status in (MitigationStatus.active, MitigationStatus.proposed)
    ]

    # Tier 1: exact scope match
    exact = [m for m in relevant if m.scope_key_id == scope_key_id and m.scope_value == scope_value]
    # Tier 2: generic fallback (both None)
    generic = [m for m in relevant if m.scope_key_id is None and m.scope_value is None]

    # Pick winner: prefer exact > generic; within tier active > proposed; then newest created_at
    winner = _pick_mitigation_winner(exact) or _pick_mitigation_winner(generic)

    if winner is None:
        return None, None

    if winner.status == MitigationStatus.active:
        # Look for a proposed in the same winning tier for proposed_mitigation_id
        tier = exact if exact else generic
        proposed_candidates = [m for m in tier if m.status == MitigationStatus.proposed]
        proposed = _pick_mitigation_winner(proposed_candidates)
        return winner.id, proposed.id if proposed else None

    # winner is proposed — no active found
    return None, winner.id


def _pick_mitigation_winner(candidates: list[MitigationView]) -> MitigationView | None:
    """From a list of mitigations, pick: active > proposed, then most recent created_at."""
    if not candidates:
        return None

    # Sort: active first (status=active < proposed alphabetically is wrong; use custom key)
    # active=0, proposed=1; then sort by created_at DESC (negate timestamp)
    def sort_key(m: MitigationView) -> tuple[int, datetime]:
        status_priority = 0 if m.status == MitigationStatus.active else 1
        return (status_priority, m.created_at)

    return min(candidates, key=lambda m: (0 if m.status == MitigationStatus.active else 1, -(m.created_at.timestamp())))


def _compute_evidence_hash(
    subject_id: UUID,
    rule_id: int,
    scope_key_id: int | None,
    scope_value: str | None,
    matched_grants: list[CapabilityGrantView],
) -> str:
    """SHA-256 hex digest over canonical JSON of stable IDs.

    Inputs: subject_id, sorted unique access_fact_ids, sorted unique initiative_ids,
    sorted unique capability_mapping_ids, rule_id, scope_key_id, scope_value.

    EffectiveGrant.id is deliberately excluded — it gets regenerated on EAS rebuild.
    Encoder contract is frozen: any change invalidates persisted Finding.evidence_hash.
    """
    access_fact_ids = sorted({fid for g in matched_grants for fid in g.source_access_fact_ids})
    initiative_ids = sorted({iid for g in matched_grants for iid in g.source_initiative_ids})
    mapping_ids = sorted({g.source_capability_mapping_id for g in matched_grants})

    payload = _serialize_canonical_json(
        subject_id=str(subject_id),
        access_fact_ids=access_fact_ids,
        initiative_ids=initiative_ids,
        capability_mapping_ids=mapping_ids,
        rule_id=rule_id,
        scope_key_id=scope_key_id,
        scope_value=scope_value,
    )
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _serialize_canonical_json(
    subject_id: str,
    access_fact_ids: list[int],
    initiative_ids: list[int],
    capability_mapping_ids: list[int],
    rule_id: int,
    scope_key_id: int | None,
    scope_value: str | None,
) -> str:
    """Serialize evidence hash inputs to canonical JSON with sorted keys.

    Keys sorted alphabetically. Separators stripped. ensure_ascii=False.
    scope_value=None serialized as JSON null (never omitted).
    """
    data = {
        'access_fact_ids': access_fact_ids,
        'capability_mapping_ids': capability_mapping_ids,
        'initiative_ids': initiative_ids,
        'rule_id': rule_id,
        'scope_key_id': scope_key_id,
        'scope_value': scope_value,
        'subject_id': subject_id,
    }
    return json.dumps(data, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public evaluate function
# ---------------------------------------------------------------------------


def evaluate(
    *,
    subject_id: UUID,
    capability_grants: list[CapabilityGrantView],
    rules: list[SodRuleView],
    mitigations: list[MitigationView],
    capability_overrides: list[CapabilityGrantView] | None = None,
    at: datetime,
) -> list[Violation]:
    """Pure SoD evaluator — deterministic, IO-free.

    Args:
        subject_id: The subject being evaluated.
        capability_grants: Active capability grants for the subject at ``at``.
        rules: All enabled SoD rules with conditions and capability_ids preloaded.
        mitigations: Subject's mitigations in (active, proposed) status whose
            validity window covers ``at``. Caller is responsible for filtering.
        capability_overrides: Optional additional grants for what-if analysis.
            Unioned with capability_grants before bucketing. No validation —
            under-specified overrides simply don't bucket.
        at: Point-in-time for which the evaluation is done (supplied by caller).

    Returns:
        Sorted list of Violation dataclasses, deterministically ordered by
        (rule_code, scope_value or '', evidence_hash).
    """
    # What-if: union overrides with grants
    effective_grants: list[CapabilityGrantView]
    if capability_overrides:
        effective_grants = list(capability_grants) + list(capability_overrides)
    else:
        effective_grants = list(capability_grants)

    violations: list[Violation] = []

    # Rules sorted by code for determinism
    sorted_rules = sorted(rules, key=lambda r: r.code)

    for rule in sorted_rules:
        # Defensive skip — caller should pre-filter enabled rules, but double-check
        if not rule.is_enabled:
            continue

        buckets = _bucket_grants(effective_grants, rule)
        bucket_scope_value: str | None

        # Determine scope_value per bucket key and rule scope_mode
        for bucket_key, bucket_grants in buckets.items():
            if rule.scope_mode == SodRuleScope.global_:
                bucket_scope_key_id: int | None = None
                bucket_scope_value = None
            elif rule.scope_mode == SodRuleScope.per_application:
                bucket_scope_key_id = None
                bucket_scope_value = None
            else:
                # BY_SCOPE_KEY — bucket_key is scope_value string
                bucket_scope_key_id = rule.scope_key_id
                bucket_scope_value = str(bucket_key)

            # Check all conditions satisfied
            matched_condition_ids: list[int] = []
            matched_slugs: set[str] = set()
            matched_grant_ids: set[int] = set()
            matched_eg_ids: set[UUID] = set()
            all_satisfied = True

            for condition in rule.conditions:
                intersecting_slugs = _intersect_distinct_slugs(bucket_grants, condition)
                if len(intersecting_slugs) < condition.min_count:
                    all_satisfied = False
                    break
                matched_condition_ids.append(condition.id)
                matched_slugs.update(intersecting_slugs)
                # Collect grant ids for matched capabilities
                for g in bucket_grants:
                    if g.capability_id in condition.capability_ids and g.capability_slug in intersecting_slugs:
                        matched_grant_ids.add(g.id)
                        matched_eg_ids.add(g.source_effective_grant_id)

            if not all_satisfied:
                continue

            # All conditions satisfied — compute evidence hash
            matched_grant_list = [g for g in bucket_grants if g.id in matched_grant_ids]
            evidence_hash = _compute_evidence_hash(
                subject_id=subject_id,
                rule_id=rule.id,
                scope_key_id=bucket_scope_key_id,
                scope_value=bucket_scope_value,
                matched_grants=matched_grant_list,
            )

            # Resolve mitigation
            active_mit_id, proposed_mit_id = _resolve_mitigation(
                rule_id=rule.id,
                subject_id=subject_id,
                scope_key_id=bucket_scope_key_id,
                scope_value=bucket_scope_value,
                mitigations=mitigations,
            )

            violations.append(
                Violation(
                    rule_id=rule.id,
                    rule_code=rule.code,
                    severity=rule.severity,
                    scope_mode=rule.scope_mode,
                    scope_key_id=bucket_scope_key_id,
                    scope_value=bucket_scope_value,
                    matched_condition_ids=sorted(matched_condition_ids),
                    matched_capability_slugs=sorted(matched_slugs),
                    matched_capability_grant_ids=sorted(matched_grant_ids),
                    matched_effective_grant_ids=sorted(matched_eg_ids, key=str),
                    evidence_hash=evidence_hash,
                    is_mitigated=active_mit_id is not None,
                    active_mitigation_id=active_mit_id,
                    proposed_mitigation_id=proposed_mit_id,
                    evaluated_at=at,
                )
            )

    # Sort violations deterministically
    violations.sort(key=lambda v: (v.rule_code, v.scope_value or '', v.evidence_hash))
    return violations

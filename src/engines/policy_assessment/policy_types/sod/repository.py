# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Read-only SQL helpers for the SoD Evaluator slice.

Three public functions — one query each (no N+1):
  load_enabled_rules     — SodRule + SodRuleCondition + M2M capability_ids
  load_subject_capability_grants — CapabilityGrant + Capability slug + EAS join
  load_subject_mitigations — Mitigation (active/proposed, window-filtered)

No writes, no events, no ORM relationships traversed.
"""

from __future__ import annotations

from datetime import datetime
import itertools
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.effective_access.models import EffectiveGrant
from src.engines.policy_assessment.policy_types.sod.evaluator import (
    CapabilityGrantView,
    MitigationView,
    SodRuleConditionView,
    SodRuleView,
)
from src.inventory.access_model.capabilities.models import Capability
from src.inventory.access_model.capability_grants.models import CapabilityGrant
from src.inventory.access_model.capability_scope_keys.models import CapabilityScopeKey
from src.inventory.assessment.mitigations.models import Mitigation, MitigationStatus
from src.inventory.policy.sod_rule_conditions.models import (
    SodRuleCondition,
    sod_rule_condition_capabilities,
)
from src.inventory.policy.sod_rules.models import SodRule
from src.platform.applications.models import Application


async def load_enabled_rules(session: AsyncSession) -> list[SodRuleView]:
    """Load all enabled SodRules with conditions and M2M capability_ids preloaded.

    Two queries total — one for rules+conditions, one for M2M links.
    """
    # Query 1: load enabled rules and their conditions
    rule_stmt = (
        sa.select(SodRule, SodRuleCondition)
        .outerjoin(SodRuleCondition, SodRuleCondition.rule_id == SodRule.id)
        .where(SodRule.is_enabled.is_(True))
        .order_by(SodRule.code, SodRuleCondition.id)
    )
    result = await session.execute(rule_stmt)
    rows = result.all()

    # Group conditions by rule
    rules_dict: dict[int, SodRule] = {}
    conditions_by_rule: dict[int, list[SodRuleCondition]] = {}
    for rule, condition in rows:
        if rule.id not in rules_dict:
            rules_dict[rule.id] = rule
            conditions_by_rule[rule.id] = []
        if condition is not None:
            conditions_by_rule[rule.id].append(condition)

    # Collect all condition ids for bulk M2M fetch
    all_condition_ids = [c.id for conditions in conditions_by_rule.values() for c in conditions]

    # Query 2: load M2M links for all conditions at once
    cap_ids_by_condition: dict[int, frozenset[int]] = {}
    if all_condition_ids:
        t = sod_rule_condition_capabilities
        m2m_stmt = (
            sa.select(t.c.condition_id, t.c.capability_id)
            .where(t.c.condition_id.in_(all_condition_ids))
            .order_by(t.c.condition_id, t.c.capability_id)
        )
        m2m_result = await session.execute(m2m_stmt)
        m2m_rows = m2m_result.all()
        for condition_id, group in itertools.groupby(m2m_rows, key=lambda r: r.condition_id):
            cap_ids_by_condition[condition_id] = frozenset(r.capability_id for r in group)

    # Build SodRuleView list
    rule_views: list[SodRuleView] = []
    for rule_id, rule in rules_dict.items():
        condition_views = tuple(
            SodRuleConditionView(
                id=c.id,
                name=c.name,
                min_count=c.min_count,
                capability_ids=cap_ids_by_condition.get(c.id, frozenset()),
            )
            for c in conditions_by_rule[rule_id]
        )
        rule_views.append(
            SodRuleView(
                id=rule.id,
                code=rule.code,
                severity=rule.severity,
                scope_mode=rule.scope_mode,
                scope_key_id=rule.scope_key_id,
                is_enabled=rule.is_enabled,
                conditions=condition_views,
            )
        )

    return rule_views


async def load_subject_capability_grants(
    session: AsyncSession,
    subject_id: UUID,
    at: datetime,
) -> list[CapabilityGrantView]:
    """Load active CapabilityGrants for subject at point-in-time ``at``.

    Active-at predicate: observed_at <= at AND (tombstoned_at IS NULL OR tombstoned_at > at).
    Joins Capability for slug. Then bulk-fetches EffectiveGrant for source arrays.
    Never per-grant queries — two queries total.
    """
    # Query 1: grants + capability slug
    grant_stmt = (
        sa.select(
            CapabilityGrant.id,
            CapabilityGrant.subject_id,
            CapabilityGrant.capability_id,
            Capability.slug.label('capability_slug'),
            CapabilityGrant.scope_key_id,
            CapabilityGrant.scope_value,
            CapabilityGrant.application_id,
            CapabilityGrant.source_effective_grant_id,
            CapabilityGrant.source_capability_mapping_id,
        )
        .join(Capability, Capability.id == CapabilityGrant.capability_id)
        .where(
            CapabilityGrant.subject_id == subject_id,
            CapabilityGrant.observed_at <= at,
            sa.or_(
                CapabilityGrant.tombstoned_at.is_(None),
                CapabilityGrant.tombstoned_at > at,
            ),
        )
        .order_by(CapabilityGrant.id)
    )
    grant_result = await session.execute(grant_stmt)
    grant_rows = grant_result.all()

    if not grant_rows:
        return []

    # Query 2: bulk-fetch EffectiveGrant source arrays by source_effective_grant_id
    eg_ids = {row.source_effective_grant_id for row in grant_rows}
    eg_stmt = sa.select(
        EffectiveGrant.id,
        EffectiveGrant.source_access_fact_id,
        EffectiveGrant.source_initiative_id,
    ).where(EffectiveGrant.id.in_(eg_ids))
    eg_result = await session.execute(eg_stmt)

    # Build lookup: UUID → (source_access_fact_ids, source_initiative_ids)
    # Note: EffectiveGrant has single source_access_fact_id and source_initiative_id per row
    # (Phase 12 columns are singular, not arrays — see models.py)
    eg_map: dict[UUID, tuple[list[int], list[int]]] = {}
    for eg_row in eg_result.all():
        # Multiple EG rows can share the same id due to partitioned parent, but in practice
        # each UUID maps to one logical row. We collect all access_fact_ids and initiative_ids.
        eg_id = eg_row.id
        # source_access_fact_id and source_initiative_id are UUIDs in Phase 09 EAS model.
        # Per TASK.md Q2: EAS columns are ARRAY(BigInteger) post-Phase-12.
        # But the actual model (effective_access/models.py) has scalar UUIDs.
        # Use the UUID as-is and convert to int representation for hash inputs if needed.
        # Actually re-reading the model: source_access_fact_id is UUID, source_initiative_id is UUID.
        # The TASK says "source_access_fact_ids: list[int]" on CapabilityGrantView.
        # Per phase_13.md the EAS was updated in Phase 12 to have ARRAY(BigInteger).
        # Looking at the actual model code: source_access_fact_id is UUID (singular).
        # We'll store the int(UUID) to satisfy list[int] — or use a hash.
        # Actually let's store them as empty lists if the EG model doesn't have arrays yet.
        # The TASK says: "EAS columns are already ARRAY(BigInteger) post-Phase-12."
        # But the model file shows scalar UUID. We'll use the UUID.int for stable int IDs.
        if eg_id not in eg_map:
            eg_map[eg_id] = ([], [])
        fact_id_int = eg_row.source_access_fact_id.int if eg_row.source_access_fact_id else None
        init_id_int = eg_row.source_initiative_id.int if eg_row.source_initiative_id else None
        if fact_id_int is not None and fact_id_int not in eg_map[eg_id][0]:
            eg_map[eg_id][0].append(fact_id_int)
        if init_id_int is not None and init_id_int not in eg_map[eg_id][1]:
            eg_map[eg_id][1].append(init_id_int)

    # Build CapabilityGrantView list
    views: list[CapabilityGrantView] = []
    for row in grant_rows:
        eg_id = row.source_effective_grant_id
        access_fact_ids, initiative_ids = eg_map.get(eg_id, ([], []))
        views.append(
            CapabilityGrantView(
                id=row.id,
                subject_id=row.subject_id,
                capability_id=row.capability_id,
                capability_slug=row.capability_slug,
                scope_key_id=row.scope_key_id,
                scope_value=row.scope_value,
                application_id=row.application_id,
                source_effective_grant_id=eg_id,
                source_capability_mapping_id=row.source_capability_mapping_id,
                source_access_fact_ids=sorted(access_fact_ids),
                source_initiative_ids=sorted(initiative_ids),
            )
        )
    return views


async def load_capability_id_and_slug(
    session: AsyncSession,
    capability_id: int,
) -> tuple[int, str] | None:
    """Return (id, slug) for the given capability_id, or None if not found.

    One-shot SELECT — no joins, no eager loads.
    """
    stmt = sa.select(Capability.id, Capability.slug).where(Capability.id == capability_id)
    result = await session.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return None
    return (row.id, row.slug)


async def scope_key_exists(
    session: AsyncSession,
    scope_key_id: int,
) -> tuple[bool, bool]:
    """Return (exists, is_global) for the given scope_key_id.

    is_global is True when the code starts with 'GLOBAL' (case-insensitive).
    Returns (False, False) when the row is not found.
    One-shot SELECT — avoids a second query in the service layer.
    """
    stmt = sa.select(CapabilityScopeKey.id, CapabilityScopeKey.code).where(CapabilityScopeKey.id == scope_key_id)
    result = await session.execute(stmt)
    row = result.one_or_none()
    if row is None:
        return (False, False)
    is_global = row.code.upper().startswith('GLOBAL')
    return (True, is_global)


async def application_exists(
    session: AsyncSession,
    application_id: UUID,
) -> bool:
    """Return True when the Application row exists, False otherwise.

    One-shot EXISTS query.
    """
    stmt = sa.select(sa.literal(1)).where(Application.id == application_id).limit(1)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


async def load_subject_mitigations(
    session: AsyncSession,
    subject_id: UUID,
    at: datetime,
) -> list[MitigationView]:
    """Load subject's mitigations in (active, proposed) status valid at ``at``.

    Validity window: valid_from <= at AND (valid_until IS NULL OR valid_until > at).
    """
    stmt = (
        sa.select(Mitigation)
        .where(
            Mitigation.subject_id == subject_id,
            Mitigation.status.in_([MitigationStatus.active, MitigationStatus.proposed]),
            Mitigation.valid_from <= at,
            sa.or_(
                Mitigation.valid_until.is_(None),
                Mitigation.valid_until > at,
            ),
        )
        .order_by(Mitigation.id)
    )
    result = await session.execute(stmt)
    rows = result.scalars().all()

    return [
        MitigationView(
            id=m.id,
            rule_id=m.rule_id,
            subject_id=m.subject_id,
            scope_key_id=m.scope_key_id,
            scope_value=m.scope_value,
            status=m.status,
            valid_from=m.valid_from,
            valid_until=m.valid_until,
            created_at=m.created_at,
        )
        for m in rows
    ]

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Idempotent apply service for config-as-code SoD rule management.

Keying rules by ``code`` and conditions by ``name`` within a rule.
Capabilities referenced by slug — resolved to IDs before upsert.
Does not commit — caller owns the transaction boundary.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.policy.sod_rule_conditions.repository import (
    delete_sod_rule_condition,
    insert_sod_rule_condition_with_capabilities,
    list_sod_rule_conditions_for_rule,
)
from src.inventory.policy.sod_rules.models import SodRule
from src.inventory.policy.sod_rules.repository import (
    insert_sod_rule,
    list_sod_rules,
)
from src.inventory.policy.sod_rules.schemas import (
    SodApplyPayload,
    SodApplyResult,
    SodRuleSpec,
)


async def _resolve_slugs(session: AsyncSession, slugs: list[str]) -> dict[str, int]:
    """Return {slug: id} for each slug found in capabilities table."""
    stmt = sa.text('SELECT id, slug FROM capabilities WHERE slug = ANY(:slugs)').bindparams(slugs=slugs)
    result = await session.execute(stmt)
    return {row[1]: row[0] for row in result.all()}


async def _sync_conditions(
    session: AsyncSession,
    rule: SodRule,
    spec: SodRuleSpec,
    slug_to_id: dict[str, int],
    result: SodApplyResult,
) -> None:
    """Sync conditions for a rule: delete stale, create missing/changed."""
    existing = await list_sod_rule_conditions_for_rule(session, rule.id)
    existing_by_name = {(row.condition.name or ''): row for row in existing}
    spec_names = {cond.name for cond in spec.conditions}

    # Delete conditions not in spec
    for name, row in existing_by_name.items():
        if name not in spec_names:
            await delete_sod_rule_condition(session, row.condition)
            result.conditions_deleted += 1

    # Create or replace conditions
    for cond_spec in spec.conditions:
        cap_ids = sorted(slug_to_id[s] for s in cond_spec.capabilities)
        existing_row = existing_by_name.get(cond_spec.name)

        if existing_row is not None:
            changed = (
                existing_row.condition.min_count != cond_spec.min_count
                or sorted(existing_row.capability_ids) != cap_ids
            )
            if not changed:
                continue
            # Replace: delete old, create new
            await delete_sod_rule_condition(session, existing_row.condition)
            result.conditions_deleted += 1

        await insert_sod_rule_condition_with_capabilities(
            session,
            rule_id=rule.id,
            name=cond_spec.name,
            min_count=cond_spec.min_count,
            capability_ids=cap_ids,
        )
        result.conditions_created += 1


async def apply_sod_rules(
    session: AsyncSession,
    payload: SodApplyPayload,
) -> SodApplyResult:
    """Idempotent upsert of SoD rules from declarative config.

    Keys rules by ``code``, conditions by ``name`` within a rule.
    Returns a diff summary. Does not commit.
    """
    result = SodApplyResult()

    # Resolve all capability slugs up front — fail fast if unknown
    all_slugs = list({s for rule in payload.rules for cond in rule.conditions for s in cond.capabilities})
    slug_to_id = await _resolve_slugs(session, all_slugs)
    unknown = [s for s in all_slugs if s not in slug_to_id]
    if unknown:
        result.unknown_capabilities = sorted(unknown)
        return result

    # Load existing rules indexed by code
    existing_rules = await list_sod_rules(session, limit=1000)
    rules_by_code = {r.code: r for r in existing_rules}

    for spec in payload.rules:
        rule = rules_by_code.get(spec.code)

        if rule is None:
            rule = await insert_sod_rule(
                session,
                code=spec.code,
                name=spec.name,
                description=spec.description,
                severity=spec.severity,
                scope_mode=spec.scope_mode,
                scope_key_id=None,
                is_enabled=spec.is_enabled,
                mitigation_allowed=spec.mitigation_allowed,
                created_by=payload.created_by,
            )
            result.rules_created += 1
        else:
            changed = (
                rule.name != spec.name
                or rule.description != spec.description
                or rule.severity != spec.severity
                or rule.scope_mode != spec.scope_mode
                or rule.is_enabled != spec.is_enabled
                or rule.mitigation_allowed != spec.mitigation_allowed
            )
            if changed:
                rule.name = spec.name
                rule.description = spec.description
                rule.severity = spec.severity
                rule.scope_mode = spec.scope_mode
                rule.is_enabled = spec.is_enabled
                rule.mitigation_allowed = spec.mitigation_allowed
                await session.flush()
                result.rules_updated += 1
            else:
                result.rules_unchanged += 1

        await _sync_conditions(session, rule, spec, slug_to_id, result)

    return result

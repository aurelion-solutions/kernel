# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRuleCondition repository — plain async functions over AsyncSession.

No commits. Service flushes; caller commits (per ARCH_CONTEXT transaction-ownership rule).

Capability resolution uses explicit SQL queries (no ORM relationships).
Decision 5: two queries for single-condition fetch; two queries for list (no N+1).
"""

from __future__ import annotations

from collections import defaultdict

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.sod_rule_conditions.models import (
    SodRuleCondition,
    sod_rule_condition_capabilities,
)
from src.capabilities.access_analysis.sod_rule_conditions.schemas import SodRuleConditionRow


async def insert_sod_rule_condition_with_capabilities(
    session: AsyncSession,
    *,
    rule_id: int,
    name: str | None,
    min_count: int,
    capability_ids: list[int],
) -> SodRuleConditionRow:
    """Insert condition row + M2M rows, flush, return hydrated DTO.

    capability_ids are returned sorted ASC from Python (no re-SELECT needed).
    """
    condition = SodRuleCondition(
        rule_id=rule_id,
        name=name,
        min_count=min_count,
    )
    session.add(condition)
    await session.flush()
    await session.refresh(condition)

    if capability_ids:
        rows = [{'condition_id': condition.id, 'capability_id': cap_id} for cap_id in capability_ids]
        await session.execute(
            sod_rule_condition_capabilities.insert(),
            rows,
        )
        await session.flush()

    return SodRuleConditionRow(
        condition=condition,
        capability_ids=sorted(capability_ids),
    )


async def get_sod_rule_condition_by_id_with_capabilities(
    session: AsyncSession,
    condition_id: int,
) -> SodRuleConditionRow | None:
    """Return condition + resolved capability_ids, or None if not found.

    Two queries: one for the condition row, one for the M2M.
    """
    stmt = sa.select(SodRuleCondition).where(SodRuleCondition.id == condition_id)
    result = await session.execute(stmt)
    condition = result.scalar_one_or_none()
    if condition is None:
        return None

    cap_stmt = (
        sa.select(sod_rule_condition_capabilities.c.capability_id)
        .where(sod_rule_condition_capabilities.c.condition_id == condition_id)
        .order_by(sod_rule_condition_capabilities.c.capability_id.asc())
    )
    cap_result = await session.execute(cap_stmt)
    capability_ids = [row[0] for row in cap_result.all()]

    return SodRuleConditionRow(condition=condition, capability_ids=capability_ids)


async def list_sod_rule_conditions_for_rule(
    session: AsyncSession,
    rule_id: int,
) -> list[SodRuleConditionRow]:
    """Return all conditions for a rule ordered by id ASC, each with resolved capability_ids.

    Two queries total regardless of N (no N+1):
    1. All conditions for the rule.
    2. All M2M rows for those conditions, grouped in Python.
    """
    cond_stmt = (
        sa.select(SodRuleCondition).where(SodRuleCondition.rule_id == rule_id).order_by(SodRuleCondition.id.asc())
    )
    cond_result = await session.execute(cond_stmt)
    conditions = list(cond_result.scalars().all())

    if not conditions:
        return []

    condition_ids = [c.id for c in conditions]
    cap_stmt = (
        sa.select(
            sod_rule_condition_capabilities.c.condition_id,
            sod_rule_condition_capabilities.c.capability_id,
        )
        .where(sod_rule_condition_capabilities.c.condition_id.in_(condition_ids))
        .order_by(
            sod_rule_condition_capabilities.c.condition_id,
            sod_rule_condition_capabilities.c.capability_id,
        )
    )
    cap_result = await session.execute(cap_stmt)
    cap_map: dict[int, list[int]] = defaultdict(list)
    for cond_id, cap_id in cap_result.all():
        cap_map[cond_id].append(cap_id)

    return [SodRuleConditionRow(condition=c, capability_ids=cap_map[c.id]) for c in conditions]


async def delete_sod_rule_condition(
    session: AsyncSession,
    condition_id: int,
) -> bool:
    """Delete a condition by id. Returns True if deleted, False if not found.

    M2M rows are cascade-deleted by DB (ON DELETE CASCADE on condition_id FK).
    """
    stmt = sa.delete(SodRuleCondition).where(SodRuleCondition.id == condition_id).returning(SodRuleCondition.id)
    result = await session.execute(stmt)
    deleted = result.scalar_one_or_none()
    await session.flush()
    return deleted is not None


async def verify_capability_ids_exist(
    session: AsyncSession,
    capability_ids: list[int],
) -> list[int]:
    """Return list of ids from capability_ids that do NOT exist in capabilities table.

    Single SELECT for all ids — no N+1.
    """
    stmt = sa.text('SELECT id FROM capabilities WHERE id = ANY(:ids)').bindparams(ids=capability_ids)
    result = await session.execute(stmt)
    found_ids = {row[0] for row in result.all()}
    return [cid for cid in capability_ids if cid not in found_ids]


async def verify_rule_id_exists(
    session: AsyncSession,
    rule_id: int,
) -> bool:
    """Return True if a SodRule with the given id exists."""
    stmt = sa.text('SELECT 1 FROM sod_rules WHERE id = :id LIMIT 1').bindparams(id=rule_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None

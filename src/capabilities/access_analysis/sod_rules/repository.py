# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRule repository — plain async functions over AsyncSession.

No commits. Service flushes; caller commits (per ARCH_CONTEXT transaction-ownership rule).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.sod_rules.models import SodRule, SodRuleScope, SodSeverity


async def insert_sod_rule(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    description: str | None,
    severity: SodSeverity,
    scope_mode: SodRuleScope,
    scope_key_id: int | None,
    is_enabled: bool,
    mitigation_allowed: bool,
    created_by: str | None,
) -> SodRule:
    """Insert a new SodRule row and flush. Does not commit."""
    rule = SodRule(
        code=code,
        name=name,
        description=description,
        severity=severity,
        scope_mode=scope_mode,
        scope_key_id=scope_key_id,
        is_enabled=is_enabled,
        mitigation_allowed=mitigation_allowed,
        created_by=created_by,
    )
    session.add(rule)
    await session.flush()
    await session.refresh(rule)
    return rule


async def get_sod_rule_by_id(
    session: AsyncSession,
    rule_id: int,
) -> SodRule | None:
    """Return the SodRule with the given id, or None."""
    stmt = sa.select(SodRule).where(SodRule.id == rule_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def list_sod_rules(
    session: AsyncSession,
    *,
    is_enabled: bool | None = None,
    severity: SodSeverity | None = None,
    scope_mode: SodRuleScope | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[SodRule]:
    """Return SodRules ordered by id ASC, optionally filtered."""
    stmt = sa.select(SodRule).order_by(SodRule.id.asc())
    if is_enabled is not None:
        stmt = stmt.where(SodRule.is_enabled == is_enabled)
    if severity is not None:
        stmt = stmt.where(SodRule.severity == severity)
    if scope_mode is not None:
        stmt = stmt.where(SodRule.scope_mode == scope_mode)
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def update_sod_rule_fields(
    session: AsyncSession,
    rule: SodRule,
    *,
    name: str | None = None,
    description: str | None = None,
    severity: SodSeverity | None = None,
    scope_mode: SodRuleScope | None = None,
    scope_key_id: int | None = None,
    is_enabled: bool | None = None,
    mitigation_allowed: bool | None = None,
    _clear_scope_key: bool = False,
) -> SodRule:
    """Update only explicitly-provided fields on the rule, flush, and return refreshed entity.

    ``_clear_scope_key=True`` is used when the caller explicitly sets scope_key_id to null.
    """
    if name is not None:
        rule.name = name
    if description is not None:
        rule.description = description
    if severity is not None:
        rule.severity = severity
    if scope_mode is not None:
        rule.scope_mode = scope_mode
    if _clear_scope_key:
        rule.scope_key_id = None
    elif scope_key_id is not None:
        rule.scope_key_id = scope_key_id
    if is_enabled is not None:
        rule.is_enabled = is_enabled
    if mitigation_allowed is not None:
        rule.mitigation_allowed = mitigation_allowed
    await session.flush()
    await session.refresh(rule)
    return rule


async def verify_scope_key_id_exists(
    session: AsyncSession,
    scope_key_id: int,
) -> bool:
    """Return True if a CapabilityScopeKey with the given id exists."""
    stmt = sa.text('SELECT 1 FROM capability_scope_keys WHERE id = :id LIMIT 1').bindparams(id=scope_key_id)
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None

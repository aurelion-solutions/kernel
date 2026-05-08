# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer tests for SodRuleConditionService."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from src.inventory.policy.sod_rule_conditions.exceptions import (
    SodRuleConditionCapabilityNotFoundError,
    SodRuleConditionEmptyCapabilitiesError,
    SodRuleConditionNotFoundError,
)
from src.inventory.policy.sod_rule_conditions.schemas import SodRuleConditionCreate
from src.inventory.policy.sod_rule_conditions.service import SodRuleConditionService
from src.inventory.policy.sod_rules.exceptions import SodRuleNotFoundError
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_cond_service(session) -> SodRuleConditionService:
    return SodRuleConditionService(session, NoOpLogService())


async def _create_capability(session, slug: str) -> int:
    result = await session.execute(
        sa.text(
            'INSERT INTO capabilities (slug, name) VALUES (:slug, :name) '
            'ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name '
            'RETURNING id'
        ).bindparams(slug=slug, name=slug)
    )
    await session.flush()
    return result.scalar_one()


async def _create_rule(session) -> int:
    result = await session.execute(
        sa.text(
            'INSERT INTO sod_rules (code, name, severity, scope_mode, is_enabled, mitigation_allowed) '
            "VALUES (:code, :name, 'high', 'global', true, true) "
            'RETURNING id'
        ).bindparams(code=f'COND-RULE-{id(session)}', name='Test Rule')
    )
    await session.flush()
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_condition_with_valid_capabilities_succeeds(session_factory) -> None:
    async with session_factory() as session:
        cap_id1 = await _create_capability(session, 'approve_payment_cond_svc1')
        cap_id2 = await _create_capability(session, 'post_journal_cond_svc1')
        rule_id = await _create_rule(session)
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        result = await svc.create(
            rule_id,
            SodRuleConditionCreate(
                capability_ids=[cap_id2, cap_id1],  # deliberately unsorted
            ),
        )
        await session.commit()

    assert result.rule_id == rule_id
    assert result.capability_ids == sorted([cap_id1, cap_id2])  # sorted ASC


@pytest.mark.asyncio
async def test_create_condition_empty_capability_ids_raises(session_factory) -> None:
    async with session_factory() as session:
        rule_id = await _create_rule(session)
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        with pytest.raises(SodRuleConditionEmptyCapabilitiesError):
            # Bypass schema validation by calling service directly
            payload = SodRuleConditionCreate.__new__(SodRuleConditionCreate)
            payload.__dict__.update({'name': None, 'min_count': 1, 'capability_ids': []})
            await svc.create(rule_id, payload)


@pytest.mark.asyncio
async def test_create_condition_unknown_capability_ids_raises(session_factory) -> None:
    async with session_factory() as session:
        cap_id = await _create_capability(session, 'known_cap_cond_svc2')
        rule_id = await _create_rule(session)
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        with pytest.raises(SodRuleConditionCapabilityNotFoundError) as exc_info:
            await svc.create(
                rule_id,
                SodRuleConditionCreate(capability_ids=[cap_id, 999998, 999999]),
            )
    assert 999998 in exc_info.value.missing_ids
    assert 999999 in exc_info.value.missing_ids


@pytest.mark.asyncio
async def test_create_condition_min_count_larger_than_capabilities_is_legal(session_factory) -> None:
    async with session_factory() as session:
        cap_id1 = await _create_capability(session, 'mincount_cap1_cond_svc')
        cap_id2 = await _create_capability(session, 'mincount_cap2_cond_svc')
        cap_id3 = await _create_capability(session, 'mincount_cap3_cond_svc')
        rule_id = await _create_rule(session)
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        result = await svc.create(
            rule_id,
            SodRuleConditionCreate(
                min_count=10,  # larger than 3 caps — legal
                capability_ids=[cap_id1, cap_id2, cap_id3],
            ),
        )
        await session.commit()

    assert result.min_count == 10
    assert len(result.capability_ids) == 3


@pytest.mark.asyncio
async def test_create_condition_missing_rule_raises(session_factory) -> None:
    async with session_factory() as session:
        cap_id = await _create_capability(session, 'missing_rule_cap_cond_svc')
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        with pytest.raises(SodRuleNotFoundError):
            await svc.create(
                999999,
                SodRuleConditionCreate(capability_ids=[cap_id]),
            )


@pytest.mark.asyncio
async def test_list_for_rule_returns_ordered_with_capabilities(session_factory) -> None:
    async with session_factory() as session:
        cap_a = await _create_capability(session, 'list_cap_a')
        cap_b = await _create_capability(session, 'list_cap_b')
        cap_c = await _create_capability(session, 'list_cap_c')
        rule_id = await _create_rule(session)
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        c1 = await svc.create(rule_id, SodRuleConditionCreate(capability_ids=[cap_a]))
        c2 = await svc.create(rule_id, SodRuleConditionCreate(capability_ids=[cap_b, cap_c]))
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        conditions = await svc.list_for_rule(rule_id)

    assert len(conditions) == 2
    assert conditions[0].id == c1.id
    assert conditions[1].id == c2.id
    assert conditions[1].capability_ids == sorted([cap_b, cap_c])


@pytest.mark.asyncio
async def test_get_missing_condition_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_cond_service(session)
        with pytest.raises(SodRuleConditionNotFoundError):
            await svc.get(999999)


@pytest.mark.asyncio
async def test_delete_condition_then_get_raises(session_factory) -> None:
    async with session_factory() as session:
        cap_id = await _create_capability(session, 'delete_cap_cond_svc')
        rule_id = await _create_rule(session)
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        created = await svc.create(rule_id, SodRuleConditionCreate(capability_ids=[cap_id]))
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        await svc.delete(created.id)
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        with pytest.raises(SodRuleConditionNotFoundError):
            await svc.get(created.id)


@pytest.mark.asyncio
async def test_delete_condition_second_delete_raises(session_factory) -> None:
    async with session_factory() as session:
        cap_id = await _create_capability(session, 'double_del_cap_cond_svc')
        rule_id = await _create_rule(session)
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        created = await svc.create(rule_id, SodRuleConditionCreate(capability_ids=[cap_id]))
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        await svc.delete(created.id)
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        with pytest.raises(SodRuleConditionNotFoundError):
            await svc.delete(created.id)


@pytest.mark.asyncio
async def test_delete_parent_rule_cascades_conditions(session_factory) -> None:
    """Deleting parent rule cascades conditions + M2M rows."""
    from src.inventory.policy.sod_rule_conditions.models import (
        SodRuleCondition,
        sod_rule_condition_capabilities,
    )
    from src.inventory.policy.sod_rules.models import SodRule

    async with session_factory() as session:
        cap_id = await _create_capability(session, 'cascade_cap_cond_svc')
        rule_id = await _create_rule(session)
        await session.commit()

    async with session_factory() as session:
        svc = await _make_cond_service(session)
        cond = await svc.create(rule_id, SodRuleConditionCreate(capability_ids=[cap_id]))
        await session.commit()
        condition_id = cond.id

    async with session_factory() as session:
        # Delete rule directly via ORM (bypassing API hard-delete restriction)
        rule = await session.get(SodRule, rule_id)
        await session.delete(rule)
        await session.commit()

    async with session_factory() as session:
        cond_count = (
            await session.execute(sa.select(sa.func.count()).where(SodRuleCondition.id == condition_id))
        ).scalar_one()
        m2m_count = (
            await session.execute(
                sa.select(sa.func.count()).where(sod_rule_condition_capabilities.c.condition_id == condition_id)
            )
        ).scalar_one()

    assert cond_count == 0
    assert m2m_count == 0

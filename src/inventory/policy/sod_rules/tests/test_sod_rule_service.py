# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer tests for SodRuleService."""

from __future__ import annotations

import pytest
from src.inventory.policy.sod_rules.exceptions import (
    SodRuleCodeAlreadyExistsError,
    SodRuleNotFoundError,
    SodRuleScopeInvariantError,
    SodRuleScopeKeyNotFoundError,
)
from src.inventory.policy.sod_rules.models import SodRuleScope, SodSeverity
from src.inventory.policy.sod_rules.schemas import SodRuleCreate, SodRulePatch
from src.inventory.policy.sod_rules.service import SodRuleService
from src.platform.logs.service import NoOpLogService

# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------


def _make_create(
    code: str = 'FIN-001',
    name: str = 'Finance Rule',
    severity: SodSeverity = SodSeverity.high,
    scope_mode: SodRuleScope = SodRuleScope.global_,
    scope_key_id: int | None = None,
) -> SodRuleCreate:
    return SodRuleCreate(
        code=code,
        name=name,
        severity=severity,
        scope_mode=scope_mode,
        scope_key_id=scope_key_id,
    )


async def _make_service(session) -> SodRuleService:
    return SodRuleService(session, NoOpLogService())


async def _create_scope_key(session) -> int:
    """Insert a CapabilityScopeKey and return its id."""
    import sqlalchemy as sa

    result = await session.execute(
        sa.text(
            "INSERT INTO capability_scope_keys (code, name) VALUES ('LEGAL_ENTITY', 'Legal Entity') "
            'ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name '
            'RETURNING id'
        )
    )
    await session.commit()
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Scope invariant tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_global_no_scope_key_succeeds(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        result = await svc.create(_make_create(code='INV-001', scope_mode=SodRuleScope.global_))
        await session.commit()
    assert result.id > 0
    assert result.scope_key_id is None


@pytest.mark.asyncio
async def test_create_global_with_scope_key_raises(session_factory) -> None:
    async with session_factory() as session:
        scope_key_id = await _create_scope_key(session)
        svc = await _make_service(session)
        with pytest.raises(SodRuleScopeInvariantError):
            await svc.create(
                _make_create(
                    code='INV-002',
                    scope_mode=SodRuleScope.global_,
                    scope_key_id=scope_key_id,
                )
            )


@pytest.mark.asyncio
async def test_create_by_scope_key_no_scope_key_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        with pytest.raises(SodRuleScopeInvariantError):
            await svc.create(
                _make_create(
                    code='INV-003',
                    scope_mode=SodRuleScope.by_scope_key,
                    scope_key_id=None,
                )
            )


@pytest.mark.asyncio
async def test_create_by_scope_key_with_valid_scope_key_succeeds(session_factory) -> None:
    async with session_factory() as session:
        scope_key_id = await _create_scope_key(session)
        svc = await _make_service(session)
        result = await svc.create(
            _make_create(
                code='INV-004',
                scope_mode=SodRuleScope.by_scope_key,
                scope_key_id=scope_key_id,
            )
        )
        await session.commit()
    assert result.scope_key_id == scope_key_id


@pytest.mark.asyncio
async def test_create_by_scope_key_with_missing_scope_key_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        with pytest.raises(SodRuleScopeKeyNotFoundError):
            await svc.create(
                _make_create(
                    code='INV-005',
                    scope_mode=SodRuleScope.by_scope_key,
                    scope_key_id=999999,
                )
            )


@pytest.mark.asyncio
async def test_create_per_application_null_scope_key_succeeds(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        result = await svc.create(
            _make_create(
                code='INV-006',
                scope_mode=SodRuleScope.per_application,
                scope_key_id=None,
            )
        )
        await session.commit()
    assert result.scope_mode == SodRuleScope.per_application


@pytest.mark.asyncio
async def test_create_per_application_with_scope_key_succeeds(session_factory) -> None:
    async with session_factory() as session:
        scope_key_id = await _create_scope_key(session)
        svc = await _make_service(session)
        result = await svc.create(
            _make_create(
                code='INV-007',
                scope_mode=SodRuleScope.per_application,
                scope_key_id=scope_key_id,
            )
        )
        await session.commit()
    assert result.scope_mode == SodRuleScope.per_application


# ---------------------------------------------------------------------------
# Duplicate code test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_duplicate_code_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        await svc.create(_make_create(code='DUP-001'))
        await session.commit()

    async with session_factory() as session:
        svc = await _make_service(session)
        with pytest.raises(SodRuleCodeAlreadyExistsError):
            await svc.create(_make_create(code='DUP-001', name='Another Name'))


# ---------------------------------------------------------------------------
# Get / list / patch / deactivate happy paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_existing_rule_returns_read(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        created = await svc.create(_make_create(code='GET-001'))
        await session.commit()
        fetched = await svc.get(created.id)
    assert fetched.id == created.id
    assert fetched.code == 'GET-001'


@pytest.mark.asyncio
async def test_get_missing_rule_raises(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        with pytest.raises(SodRuleNotFoundError):
            await svc.get(999999)


@pytest.mark.asyncio
async def test_list_with_severity_filter(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        await svc.create(_make_create(code='LIST-H1', severity=SodSeverity.high))
        await svc.create(_make_create(code='LIST-L1', severity=SodSeverity.low))
        await session.commit()
        highs = await svc.list(severity=SodSeverity.high)
        lows = await svc.list(severity=SodSeverity.low)

    high_codes = [r.code for r in highs]
    low_codes = [r.code for r in lows]
    assert 'LIST-H1' in high_codes
    assert 'LIST-L1' not in high_codes
    assert 'LIST-L1' in low_codes


@pytest.mark.asyncio
async def test_list_with_scope_mode_filter(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        await svc.create(_make_create(code='SM-G1', scope_mode=SodRuleScope.global_))
        await svc.create(_make_create(code='SM-P1', scope_mode=SodRuleScope.per_application))
        await session.commit()
        globals_ = await svc.list(scope_mode=SodRuleScope.global_)
        per_apps = await svc.list(scope_mode=SodRuleScope.per_application)

    assert any(r.code == 'SM-G1' for r in globals_)
    assert not any(r.code == 'SM-G1' for r in per_apps)


@pytest.mark.asyncio
async def test_list_with_is_enabled_filter(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        r = await svc.create(_make_create(code='EN-001'))
        await session.commit()
        await svc.deactivate(r.id)
        await session.commit()
        enabled = await svc.list(is_enabled=True)
        disabled = await svc.list(is_enabled=False)

    enabled_codes = [r.code for r in enabled]
    disabled_codes = [r.code for r in disabled]
    assert 'EN-001' not in enabled_codes
    assert 'EN-001' in disabled_codes


@pytest.mark.asyncio
async def test_patch_name_updates_field(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        created = await svc.create(_make_create(code='PATCH-001'))
        await session.commit()
        patched = await svc.patch(created.id, SodRulePatch(name='Updated'))
        await session.commit()
    assert patched.name == 'Updated'
    assert patched.code == 'PATCH-001'  # code immutable


@pytest.mark.asyncio
async def test_deactivate_is_idempotent(session_factory) -> None:
    async with session_factory() as session:
        svc = await _make_service(session)
        created = await svc.create(_make_create(code='IDMP-001'))
        await session.commit()

    async with session_factory() as session:
        svc = await _make_service(session)
        r1 = await svc.deactivate(created.id)
        await session.commit()
        r2 = await svc.deactivate(created.id)
        await session.commit()

    assert r1.is_enabled is False
    assert r2.is_enabled is False


# ---------------------------------------------------------------------------
# Patch scope-mode invariant edge cases (per Architect Decision 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_scope_mode_global_without_clearing_scope_key_raises(session_factory) -> None:
    """PATCH {scope_mode: 'global'} on a BY_SCOPE_KEY rule → invariant error."""
    async with session_factory() as session:
        scope_key_id = await _create_scope_key(session)
        svc = await _make_service(session)
        created = await svc.create(
            _make_create(
                code='PATCH-INV-001',
                scope_mode=SodRuleScope.by_scope_key,
                scope_key_id=scope_key_id,
            )
        )
        await session.commit()

    async with session_factory() as session:
        svc = await _make_service(session)
        with pytest.raises(SodRuleScopeInvariantError):
            # Switching to global without clearing scope_key_id → invariant violation
            await svc.patch(created.id, SodRulePatch(scope_mode=SodRuleScope.global_))


@pytest.mark.asyncio
async def test_patch_by_scope_key_without_scope_key_raises(session_factory) -> None:
    """PATCH {scope_mode: 'by_scope_key'} on a GLOBAL rule → invariant error."""
    async with session_factory() as session:
        svc = await _make_service(session)
        created = await svc.create(_make_create(code='PATCH-INV-002', scope_mode=SodRuleScope.global_))
        await session.commit()

    async with session_factory() as session:
        svc = await _make_service(session)
        with pytest.raises(SodRuleScopeInvariantError):
            await svc.patch(created.id, SodRulePatch(scope_mode=SodRuleScope.by_scope_key))


@pytest.mark.asyncio
async def test_patch_scope_key_id_alone_on_by_scope_key_rule_succeeds(session_factory) -> None:
    """PATCH {scope_key_id: <new>} alone on a BY_SCOPE_KEY rule → valid."""
    async with session_factory() as session:
        scope_key_id = await _create_scope_key(session)
        svc = await _make_service(session)
        created = await svc.create(
            _make_create(
                code='PATCH-INV-003',
                scope_mode=SodRuleScope.by_scope_key,
                scope_key_id=scope_key_id,
            )
        )
        await session.commit()

    async with session_factory() as session:
        svc = await _make_service(session)
        patched = await svc.patch(created.id, SodRulePatch(scope_key_id=scope_key_id))
        await session.commit()

    assert patched.scope_key_id == scope_key_id


@pytest.mark.asyncio
async def test_patch_scope_key_null_on_by_scope_key_rule_raises(session_factory) -> None:
    """PATCH {scope_key_id: null} alone on a BY_SCOPE_KEY rule → invariant error."""
    async with session_factory() as session:
        scope_key_id = await _create_scope_key(session)
        svc = await _make_service(session)
        created = await svc.create(
            _make_create(
                code='PATCH-INV-004',
                scope_mode=SodRuleScope.by_scope_key,
                scope_key_id=scope_key_id,
            )
        )
        await session.commit()

    async with session_factory() as session:
        svc = await _make_service(session)
        with pytest.raises(SodRuleScopeInvariantError):
            await svc.patch(created.id, SodRulePatch(scope_key_id=None))

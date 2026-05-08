# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRule API routes.

Endpoints:
  POST   /sod-rules/apply                   — config-as-code idempotent upsert
  POST   /sod-rules                         — create
  GET    /sod-rules                         — list (filters: is_enabled, severity, scope_mode)
  GET    /sod-rules/{rule_id}               — get by id
  PATCH  /sod-rules/{rule_id}              — partial update (code immutable)
  POST   /sod-rules/{rule_id}/deactivate   — soft-delete (idempotent)

No hard DELETE — rules are soft-deleted only; future findings reference rule_id.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.core.http.errors import translate_service_errors
from src.inventory.policy.sod_rules.apply_service import apply_sod_rules
from src.inventory.policy.sod_rules.deps import get_sod_rule_service
from src.inventory.policy.sod_rules.exceptions import (
    SodRuleCodeAlreadyExistsError,
    SodRuleNotFoundError,
    SodRuleScopeInvariantError,
    SodRuleScopeKeyNotFoundError,
)
from src.inventory.policy.sod_rules.models import SodRuleScope, SodSeverity
from src.inventory.policy.sod_rules.schemas import (
    SodApplyPayload,
    SodApplyResult,
    SodRuleCreate,
    SodRulePatch,
    SodRuleRead,
)
from src.inventory.policy.sod_rules.service import SodRuleService

router = APIRouter(prefix='/sod-rules', tags=['sod-rules'])
DependsService = Depends(get_sod_rule_service)


@router.post('/apply', response_model=SodApplyResult)
async def apply_sod_rules_endpoint(
    body: SodApplyPayload,
    session: AsyncSession = Depends(get_db),  # noqa: B008
) -> SodApplyResult:
    """Idempotent config-as-code upsert for SoD rules.

    Rules are keyed by ``code``; conditions by ``name`` within a rule.
    Capabilities referenced by slug. Returns 422 if any slug is unknown.
    """
    result = await apply_sod_rules(session, body)
    if result.unknown_capabilities:
        raise HTTPException(
            status_code=422,
            detail=f'Unknown capability slugs: {result.unknown_capabilities}',
        )
    await session.commit()
    return result


@router.post('', response_model=SodRuleRead, status_code=201)
async def create_sod_rule(
    body: SodRuleCreate,
    service: SodRuleService = DependsService,
) -> SodRuleRead:
    """Create a new SoD rule."""
    with translate_service_errors(
        {
            SodRuleCodeAlreadyExistsError: (
                409,
                lambda e: f"SodRule with code '{e.code}' already exists",
            ),
            SodRuleScopeInvariantError: (422, lambda e: str(e)),
            SodRuleScopeKeyNotFoundError: (
                422,
                lambda e: f'CapabilityScopeKey {e.scope_key_id} not found',
            ),
        }
    ):
        result = await service.create(body)
    return result


@router.get('', response_model=list[SodRuleRead])
async def list_sod_rules(
    is_enabled: bool | None = None,
    severity: SodSeverity | None = None,
    scope_mode: SodRuleScope | None = None,
    limit: int = 100,
    offset: int = 0,
    service: SodRuleService = DependsService,
) -> list[SodRuleRead]:
    """List SoD rules, optionally filtered. Max limit 500."""
    effective_limit = min(limit, 500)
    return await service.list(
        is_enabled=is_enabled,
        severity=severity,
        scope_mode=scope_mode,
        limit=effective_limit,
        offset=offset,
    )


@router.get('/{rule_id}', response_model=SodRuleRead)
async def get_sod_rule(
    rule_id: int,
    service: SodRuleService = DependsService,
) -> SodRuleRead:
    """Get a SoD rule by id. Returns 404 if not found."""
    with translate_service_errors(
        {
            SodRuleNotFoundError: (404, 'SodRule not found'),
        }
    ):
        result = await service.get(rule_id)
    return result


@router.patch('/{rule_id}', response_model=SodRuleRead)
async def patch_sod_rule(
    rule_id: int,
    body: SodRulePatch,
    service: SodRuleService = DependsService,
) -> SodRuleRead:
    """Partially update a SoD rule. Code is immutable and cannot be changed."""
    with translate_service_errors(
        {
            SodRuleNotFoundError: (404, 'SodRule not found'),
            SodRuleScopeInvariantError: (422, lambda e: str(e)),
            SodRuleScopeKeyNotFoundError: (
                422,
                lambda e: f'CapabilityScopeKey {e.scope_key_id} not found',
            ),
        }
    ):
        result = await service.patch(rule_id, body)
    return result


@router.post('/{rule_id}/deactivate', response_model=SodRuleRead)
async def deactivate_sod_rule(
    rule_id: int,
    service: SodRuleService = DependsService,
) -> SodRuleRead:
    """Soft-delete a SoD rule by setting is_enabled=False. Idempotent."""
    with translate_service_errors(
        {
            SodRuleNotFoundError: (404, 'SodRule not found'),
        }
    ):
        result = await service.deactivate(rule_id)
    return result

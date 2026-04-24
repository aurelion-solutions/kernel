# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SodRuleCondition API routes.

Endpoints nested under /sod-rules/{rule_id}/conditions:
  POST   /sod-rules/{rule_id}/conditions                          — create
  GET    /sod-rules/{rule_id}/conditions                          — list
  GET    /sod-rules/{rule_id}/conditions/{condition_id}           — get by id
  DELETE /sod-rules/{rule_id}/conditions/{condition_id}           — delete (204)

No PATCH — conditions are immutable; replace = DELETE + POST.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi import Response as FastAPIResponse
from src.capabilities.access_analysis.sod_rule_conditions.deps import get_sod_rule_condition_service
from src.capabilities.access_analysis.sod_rule_conditions.exceptions import (
    SodRuleConditionCapabilityNotFoundError,
    SodRuleConditionEmptyCapabilitiesError,
    SodRuleConditionNotFoundError,
)
from src.capabilities.access_analysis.sod_rule_conditions.schemas import (
    SodRuleConditionCreate,
    SodRuleConditionRead,
)
from src.capabilities.access_analysis.sod_rule_conditions.service import SodRuleConditionService
from src.capabilities.access_analysis.sod_rules.exceptions import SodRuleNotFoundError
from src.core.http.errors import translate_service_errors

router = APIRouter(prefix='/sod-rules/{rule_id}/conditions', tags=['sod-rule-conditions'])
DependsService = Depends(get_sod_rule_condition_service)


@router.post('', response_model=SodRuleConditionRead, status_code=201)
async def create_sod_rule_condition(
    rule_id: int,
    body: SodRuleConditionCreate,
    service: SodRuleConditionService = DependsService,
) -> SodRuleConditionRead:
    """Create a new condition for the given SoD rule."""
    with translate_service_errors(
        {
            SodRuleNotFoundError: (404, 'SodRule not found'),
            SodRuleConditionEmptyCapabilitiesError: (
                422,
                'capability_ids must not be empty',
            ),
            SodRuleConditionCapabilityNotFoundError: (
                422,
                lambda e: f'Capabilities not found: {e.missing_ids}',
            ),
        }
    ):
        result = await service.create(rule_id, body)
    return result


@router.get('', response_model=list[SodRuleConditionRead])
async def list_sod_rule_conditions(
    rule_id: int,
    service: SodRuleConditionService = DependsService,
) -> list[SodRuleConditionRead]:
    """List all conditions for a SoD rule."""
    return await service.list_for_rule(rule_id)


@router.get('/{condition_id}', response_model=SodRuleConditionRead)
async def get_sod_rule_condition(
    rule_id: int,
    condition_id: int,
    service: SodRuleConditionService = DependsService,
) -> SodRuleConditionRead:
    """Get a condition by id. Returns 404 if not found."""
    with translate_service_errors(
        {
            SodRuleConditionNotFoundError: (404, 'SodRuleCondition not found'),
        }
    ):
        result = await service.get(condition_id)
    return result


@router.delete('/{condition_id}', status_code=204)
async def delete_sod_rule_condition(
    rule_id: int,
    condition_id: int,
    service: SodRuleConditionService = DependsService,
) -> FastAPIResponse:
    """Delete a condition by id. Returns 204 on success, 404 if not found."""
    with translate_service_errors(
        {
            SodRuleConditionNotFoundError: (404, 'SodRuleCondition not found'),
        }
    ):
        await service.delete(condition_id)
    return FastAPIResponse(status_code=204)

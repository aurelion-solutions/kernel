# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Action API routes (read-only)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from src.inventory.actions.deps import get_action_service
from src.inventory.actions.schemas import ActionRead
from src.inventory.actions.service import ActionService

router = APIRouter(prefix='/actions', tags=['actions'])
DependsService = Depends(get_action_service)


@router.get('', response_model=list[ActionRead])
async def list_actions(
    service: ActionService = DependsService,
) -> list[ActionRead]:
    """List all actions in the reference vocabulary, ordered by id."""
    return await service.list_actions()


@router.get('/{slug}', response_model=ActionRead)
async def get_action(
    slug: str,
    service: ActionService = DependsService,
) -> ActionRead:
    """Get an action by slug. Returns 404 if not found.

    Lookup is case-sensitive. The seeded vocabulary is lowercase by contract.
    """
    action = await service.get_action_by_slug(slug)
    if action is None:
        raise HTTPException(status_code=404, detail='Action not found')
    return action

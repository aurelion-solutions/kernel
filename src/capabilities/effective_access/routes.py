# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Effective Access Store — read-only HTTP endpoints (Phase 09 Step 4).

Three endpoints, all ``GET``, all under ``/effective-grants``:

  - ``GET /effective-grants``          — list with filters + mandatory-filter guard
  - ``GET /effective-grants/explain``  — deny-wins aggregation for a triple
  - ``GET /effective-grants/{grant_id}`` — single row by id (admin/debug)

.. warning::

    **Route declaration order matters.**  ``/explain`` MUST be declared before
    ``/{grant_id}``; otherwise FastAPI would attempt to parse the literal string
    ``explain`` as a UUID and return 422 instead of routing to the explain handler.
    The route-ordering pin test (``test_routes.py::test_route_ordering_explain_not_uuid``)
    enforces this invariant.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from src.capabilities.effective_access.deps import get_effective_access_read_service
from src.capabilities.effective_access.models import EffectiveGrantEffect
from src.capabilities.effective_access.schemas import (
    EffectiveGrantExplainResponse,
    EffectiveGrantRead,
)
from src.capabilities.effective_access.service import EffectiveAccessReadService
from src.inventory.enums import Action
from src.inventory.initiatives.models import InitiativeType
from src.inventory.subjects.models import SubjectKind

router = APIRouter(prefix='/effective-grants', tags=['effective-grants'])

DependsService = Depends(get_effective_access_read_service)


# ---------------------------------------------------------------------------
# 1. List  (declared first — most general, no path segment)
# ---------------------------------------------------------------------------


@router.get('', response_model=list[EffectiveGrantRead])
async def list_effective_grants(
    subject_id: uuid.UUID | None = None,
    subject_kind: SubjectKind | None = None,
    application_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    action: Action | None = None,
    effect: EffectiveGrantEffect | None = None,
    initiative_type: InitiativeType | None = None,
    initiative_origin: str | None = Query(default=None, max_length=1024),
    source_initiative_id: uuid.UUID | None = None,
    active_only: bool = Query(default=True),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    service: EffectiveAccessReadService = DependsService,
) -> list[EffectiveGrantRead]:
    """List effective grants matching the given filters.

    **Mandatory-filter rule**: at least one of ``subject_id``, ``resource_id``,
    ``application_id``, or ``source_initiative_id`` must be provided.  Requests
    with none of these raise ``HTTP 400`` to prevent unbounded full-table scans
    across the partitioned store.

    ``active_only=true`` (default) restricts results to rows where
    ``tombstoned_at IS NULL AND (valid_until IS NULL OR valid_until > now)``.

    Results are ordered ``observed_at DESC, id DESC``.
    Pagination via ``limit`` (default 100, max 1000) and ``offset`` (default 0).
    """
    if subject_id is None and resource_id is None and application_id is None and source_initiative_id is None:
        raise HTTPException(
            status_code=400,
            detail='at least one of subject_id, resource_id, application_id, source_initiative_id is required',
        )
    rows = await service.list_grants(
        subject_id=subject_id,
        subject_kind=subject_kind,
        application_id=application_id,
        account_id=account_id,
        resource_id=resource_id,
        action=action,
        effect=effect,
        initiative_type=initiative_type,
        initiative_origin=initiative_origin,
        source_initiative_id=source_initiative_id,
        active_only=active_only,
        limit=limit,
        offset=offset,
    )
    return [EffectiveGrantRead.model_validate(r) for r in rows]


# ---------------------------------------------------------------------------
# 2. Explain  (MUST be declared before /{grant_id} — see module docstring)
# ---------------------------------------------------------------------------


@router.get('/explain', response_model=EffectiveGrantExplainResponse)
async def explain_access(
    subject_id: uuid.UUID,
    resource_id: uuid.UUID,
    action: Action,
    active_only: bool = Query(default=True),
    service: EffectiveAccessReadService = DependsService,
) -> EffectiveGrantExplainResponse:
    """Return the current projection state for the given ``(subject, resource, action)`` triple.

    The ``effect`` field is the deny-wins aggregation over matching rows:

    - ``'none'``  — no active matching rows exist
    - ``'allow'`` — all active matches carry ``effect=allow``
    - ``'deny'``  — at least one active match carries ``effect=deny``

    .. warning::

        This is a **READ-LAYER aggregation**, not a policy decision.
        PDP (Phase 06) is authoritative for allow/deny verdicts.
        ``/explain`` reports raw projection state, not policy output.
    """
    result = await service.explain_access(
        subject_id=subject_id,
        resource_id=resource_id,
        action=action,
        active_only=active_only,
    )
    return EffectiveGrantExplainResponse.model_validate(result, from_attributes=True)


# ---------------------------------------------------------------------------
# 3. By id  (MUST be declared after /explain — see module docstring)
# ---------------------------------------------------------------------------


@router.get('/{grant_id}', response_model=EffectiveGrantRead)
async def get_effective_grant(
    grant_id: uuid.UUID,
    service: EffectiveAccessReadService = DependsService,
) -> EffectiveGrantRead:
    """Fetch a single effective grant by id.

    .. warning::

        The primary key is 3-column ``(id, subject_kind, application_id)``.  A
        lookup by ``id`` alone must scan all 12 partitions (3 kinds × 4 hash
        buckets each).  This is acceptable for admin/debug traffic.
        **Do not call from a hot path.**
    """
    row = await service.get_grant(grant_id)
    if row is None:
        raise HTTPException(status_code=404, detail='Effective grant not found')
    return EffectiveGrantRead.model_validate(row)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Display enrichment for the access_plan flat-items list endpoint.

Resolves human-readable fields from the PlanItemRead data:
- subject_display  — employee full_name or NHI external_id
- application_code — PlanItem.application (may already be a code; if UUID → resolve)
- target_display   — short label from kind + target_descriptor
- change_summary   — op verb from kind

At most one SELECT per entity type — never N+1.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.access_plan.schemas import PlanItemRead
from src.inventory.display_lookups import (
    batch_application_display,
    batch_application_display_by_code,
    batch_employee_display,
    batch_nhi_display,
)
from src.inventory.subjects.models import Subject

# ---------------------------------------------------------------------------
# Subject resolution helpers
# ---------------------------------------------------------------------------


async def _batch_subject_principal_ids(
    session: AsyncSession,
    subject_uuids: set[UUID],
) -> dict[UUID, tuple[UUID | None, UUID | None]]:
    """Return {subject_id: (principal_employee_id, principal_nhi_id)} for given subject UUIDs."""
    if not subject_uuids:
        return {}
    stmt = sa.select(Subject.id, Subject.principal_employee_id, Subject.principal_nhi_id).where(
        Subject.id.in_(subject_uuids)
    )
    result = await session.execute(stmt)
    return {row.id: (row.principal_employee_id, row.principal_nhi_id) for row in result.all()}


# ---------------------------------------------------------------------------
# Application code resolution
# ---------------------------------------------------------------------------


def _is_uuid_string(value: str) -> UUID | None:
    """Return UUID if value looks like a UUID, else None."""
    try:
        return UUID(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# target_display builder
# ---------------------------------------------------------------------------


def build_target_display(kind: str, descriptor: dict[str, Any]) -> str | None:
    """Return a short human-readable target label from kind + descriptor."""
    if kind in ('grant_role', 'revoke_role'):
        role = descriptor.get('role')
        if role:
            return f'role: {role}'
    elif kind in ('group_add', 'group_remove'):
        group = descriptor.get('group')
        if group:
            return f'group: {group}'
    elif kind in ('entitlement_attach', 'entitlement_detach'):
        slug = descriptor.get('slug') or descriptor.get('entitlement')
        if slug:
            return f'entitlement: {slug}'
    elif kind in (
        'account_create',
        'account_invite',
        'account_activate',
        'account_suspend',
        'account_disable',
    ):
        account_ref = descriptor.get('account_ref') or descriptor.get('username') or descriptor.get('account')
        if account_ref:
            return f'account: {account_ref}'
        return 'account'
    return None


# ---------------------------------------------------------------------------
# change_summary builder
# ---------------------------------------------------------------------------

_KIND_SUMMARIES: dict[str, str] = {
    'account_create': '+ create account',
    'account_invite': '+ invite',
    'account_activate': '↻ activate',
    'account_suspend': '⏸ suspend',
    'account_disable': '⊘ disable',
}

_ADDITIVE_KINDS = frozenset({'grant_role', 'group_add', 'entitlement_attach'})
_REVOKE_KINDS = frozenset({'revoke_role', 'group_remove', 'entitlement_detach'})


def build_change_summary(kind: str, target_display: str | None) -> str | None:
    """Return a short change description based on kind."""
    if kind in _KIND_SUMMARIES:
        return _KIND_SUMMARIES[kind]
    if kind in _ADDITIVE_KINDS:
        return f'+ {target_display}' if target_display else '+ grant'
    if kind in _REVOKE_KINDS:
        return f'- {target_display}' if target_display else '- revoke'
    return None


# ---------------------------------------------------------------------------
# Main enrichment
# ---------------------------------------------------------------------------


async def enrich_plan_items(
    session: AsyncSession,
    items: list[PlanItemRead],
) -> list[PlanItemRead]:
    """Enrich a list of PlanItemRead with display fields.

    subject_ref is a Subject.id UUID string — resolved to principal employee/NHI
    via subjects table, then to full_name/external_id.

    application field may be a short code (e.g. "GHE") or a UUID string.
    If it's a UUID, resolve to application.code; otherwise use as-is.
    """
    # --- subject resolution ---
    subject_uuids: set[UUID] = set()
    for item in items:
        try:
            subject_uuids.add(UUID(item.subject_ref))
        except ValueError:
            pass

    principal_map = await _batch_subject_principal_ids(session, subject_uuids)

    employee_ids: set[UUID] = set()
    nhi_ids: set[UUID] = set()
    for _, (emp_id, nhi_id) in principal_map.items():
        if emp_id is not None:
            employee_ids.add(emp_id)
        if nhi_id is not None:
            nhi_ids.add(nhi_id)

    emp_display = await batch_employee_display(session, employee_ids)
    nhi_display = await batch_nhi_display(session, nhi_ids)

    # subject_id → display
    subject_display_map: dict[UUID, str] = {}
    for subject_id, (emp_id, nhi_id) in principal_map.items():
        if emp_id is not None and emp_id in emp_display:
            subject_display_map[subject_id] = emp_display[emp_id]
        elif nhi_id is not None and nhi_id in nhi_display:
            subject_display_map[subject_id] = nhi_display[nhi_id]

    # --- application code resolution ---
    # Separate items into UUID-based (resolve by id) and code-based (reverse-lookup by code).
    app_id_set: set[UUID] = set()
    app_code_set: set[str] = set()
    for item in items:
        app_uuid = _is_uuid_string(item.application)
        if app_uuid is not None:
            app_id_set.add(app_uuid)
        elif item.application:
            app_code_set.add(item.application)

    # Two parallel lookups — one SELECT per path.
    app_map, app_code_map = await asyncio.gather(
        batch_application_display(session, app_id_set),
        batch_application_display_by_code(session, app_code_set),
    )

    # Build per-item application_code / application_name.
    app_codes: list[str | None] = []
    app_names: list[str | None] = []
    for item in items:
        app_uuid = _is_uuid_string(item.application)
        if app_uuid is not None:
            app_display = app_map.get(app_uuid)
            app_codes.append(app_display.code if app_display else None)
            app_names.append(app_display.name if app_display else None)
        elif item.application:
            app_display = app_code_map.get(item.application)
            app_codes.append(item.application)  # always preserve raw code
            app_names.append(app_display.name if app_display else None)
        else:
            app_codes.append(None)
            app_names.append(None)

    # --- per-item display fields ---
    enriched: list[PlanItemRead] = []
    for idx, item in enumerate(items):
        subject_uuid = _is_uuid_string(item.subject_ref)
        s_display = subject_display_map.get(subject_uuid) if subject_uuid else None

        t_display = build_target_display(item.kind, item.target_descriptor)
        c_summary = build_change_summary(item.kind, t_display)

        enriched.append(
            item.model_copy(
                update={
                    'subject_display': s_display,
                    'application_code': app_codes[idx],
                    'application_name': app_names[idx],
                    'target_display': t_display,
                    'change_summary': c_summary,
                }
            )
        )

    return enriched

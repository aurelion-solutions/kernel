# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Display enrichment helpers for the inventory_reconcile engine.

Produces human-readable ``*_display`` fields for the cross-run delta items
list endpoint.  Batch approach: collect unique UUIDs, fire one SELECT per
entity type, map results back to each row.  No N+1 queries.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaOperation,
    ReconciliationEntityType,
)
from src.engines.inventory_reconcile.schemas import ReconciliationDeltaItemRead
from src.inventory.display_lookups import (
    batch_account_display,
    batch_application_display,
    batch_display_by_subject_ids,
    batch_resource_display,
)

# ---------------------------------------------------------------------------
# change_summary builder
# ---------------------------------------------------------------------------


def _pick_label(data: dict[str, Any] | None) -> str | None:
    """Extract the most distinctive label from a before/after JSON dict."""
    if not data:
        return None
    for key in ('role', 'group', 'email', 'slug', 'name', 'entitlement'):
        val = data.get(key)
        if val and isinstance(val, str):
            return str(val)
    return None


def build_change_summary(
    operation: str,
    before_json: dict[str, Any] | None,
    after_json: dict[str, Any] | None,
    effect: str | None,
) -> str | None:
    """Return a short human-readable description of the change."""
    op = operation.lower()

    if op == ReconciliationDeltaOperation.noop:
        return 'unchanged'

    if op == ReconciliationDeltaOperation.create:
        label = _pick_label(after_json)
        if label:
            return f'+ {label}'
        if effect:
            return f'+ {effect}'
        return '+ created'

    if op == ReconciliationDeltaOperation.revoke:
        label = _pick_label(before_json) or _pick_label(after_json)
        if label:
            return f'- {label}'
        return '- revoked'

    if op == ReconciliationDeltaOperation.update:
        before_label = _pick_label(before_json)
        after_label = _pick_label(after_json)
        if before_label and after_label and before_label != after_label:
            return f'{before_label} → {after_label}'
        # Count changed keys
        before_keys = set(before_json or {})
        after_keys = set(after_json or {})
        changed = before_keys.symmetric_difference(after_keys) | {
            k for k in before_keys & after_keys if (before_json or {}).get(k) != (after_json or {}).get(k)
        }
        if changed:
            return f'{len(changed)} fields changed'
        return 'updated'

    if op == ReconciliationDeltaOperation.reactivate:
        label = _pick_label(after_json) or _pick_label(before_json)
        if label:
            return f'↻ {label}'
        return '↻ reactivated'

    return None


# ---------------------------------------------------------------------------
# Subject display resolver
# ---------------------------------------------------------------------------


async def _resolve_subject_display(
    session: AsyncSession,
    items: list[ReconciliationDeltaItem],
) -> dict[UUID, str]:
    """Return {uuid: display_name} for all subject/entity UUIDs across items.

    For access_fact rows: subject_id is a subjects.id — resolved via
    batch_display_by_subject_ids (subjects → employees/nhis JOIN).
    For employee rows: entity_id is also a subjects.id — same path.
    For other entity_types: not resolved.
    """
    subject_ids: set[UUID] = set()

    for item in items:
        if item.entity_type == ReconciliationEntityType.access_fact and item.subject_id is not None:
            subject_ids.add(item.subject_id)
        elif item.entity_type == ReconciliationEntityType.employee and item.entity_id is not None:
            subject_ids.add(item.entity_id)

    return await batch_display_by_subject_ids(session, subject_ids)


# ---------------------------------------------------------------------------
# Main enrichment function
# ---------------------------------------------------------------------------


async def enrich_delta_items(
    session: AsyncSession,
    rows: list[tuple[ReconciliationDeltaItem, UUID | None]],
) -> list[ReconciliationDeltaItemRead]:
    """Enrich a page of (delta_item, application_id) rows with display fields.

    Issues at most one SELECT per entity type (employees, nhis, accounts,
    resources, applications) — never N+1.
    """
    items = [item for item, _ in rows]

    # --- collect unique IDs ---
    account_ids: set[UUID] = {item.account_id for item in items if item.account_id is not None}
    # For account-delta rows entity_id points to an existing account (update/revoke/reactivate/noop).
    # Include those in the batch so the map can resolve account_display via entity_id.
    account_entity_ids: set[UUID] = {
        item.entity_id
        for item in items
        if item.entity_type == ReconciliationEntityType.account and item.entity_id is not None
    }
    resource_ids: set[UUID] = {item.resource_id for item in items if item.resource_id is not None}
    app_ids: set[UUID] = {app_id for _, app_id in rows if app_id is not None}

    # --- batch lookups (parallel via sequential awaits — each is one query) ---
    subject_map = await _resolve_subject_display(session, items)
    account_map = await batch_account_display(session, account_ids | account_entity_ids)
    resource_map = await batch_resource_display(session, resource_ids)
    app_map = await batch_application_display(session, app_ids)

    # --- map each row ---
    result: list[ReconciliationDeltaItemRead] = []
    for item, app_id in rows:
        # subject_display
        if item.entity_type == ReconciliationEntityType.access_fact and item.subject_id is not None:
            subject_display = subject_map.get(item.subject_id)
        elif item.entity_type == ReconciliationEntityType.employee and item.entity_id is not None:
            subject_display = subject_map.get(item.entity_id)
        elif item.entity_type == ReconciliationEntityType.account:
            # For account deltas: use account_id (if already exists) or entity_id as fallback.
            # For create operations entity_id is None (account not yet in DB).
            ref_id = item.account_id or item.entity_id
            if ref_id is not None:
                subject_display = account_map.get(ref_id)
            else:
                subject_display = _resolve_account_display_from_json(item)
        else:
            subject_display = None

        app_display = app_map.get(app_id) if app_id else None

        # account_display fallback chain:
        # 1. account_id FK → accounts.username
        # 2. entity_id (existing account) → accounts.username
        # 3. after_json['username'] (create — account not yet in DB)
        # 4. before_json['username'] (revoke — after may be absent)
        # 5. None
        if item.entity_type == ReconciliationEntityType.account:
            account_display = account_map.get(item.account_id) if item.account_id is not None else None
            if account_display is None and item.entity_id is not None:
                account_display = account_map.get(item.entity_id)
            if account_display is None:
                account_display = _resolve_account_display_from_json(item)
        else:
            account_display = account_map.get(item.account_id) if item.account_id is not None else None

        read = ReconciliationDeltaItemRead.model_validate(item).model_copy(
            update={
                'application_id': app_id,
                'subject_display': subject_display,
                'account_display': account_display,
                'resource_display': resource_map.get(item.resource_id) if item.resource_id else None,
                'application_code': app_display.code if app_display else None,
                'application_name': app_display.name if app_display else None,
                'change_summary': build_change_summary(
                    item.operation,
                    item.before_json,
                    item.after_json,
                    item.effect,
                ),
            }
        )
        result.append(read)

    return result


def _resolve_account_display_from_json(item: ReconciliationDeltaItem) -> str | None:
    """Extract username from after_json or before_json.

    Fallback for cases where account is not yet persisted in the DB (create)
    or entity_id is unavailable.  Checks after_json first, then before_json.
    """
    after: dict[str, Any] | None = item.after_json
    before: dict[str, Any] | None = item.before_json
    username: str | None = (after or {}).get('username') or (before or {}).get('username')
    return username or None

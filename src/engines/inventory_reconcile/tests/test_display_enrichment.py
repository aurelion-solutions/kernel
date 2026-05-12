# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for inventory_reconcile display_enrichment module.

Covers:
- build_change_summary function (pure, no DB)
- enrich_delta_items integration (batch lookup, no N+1)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from src.engines.inventory_reconcile.display_enrichment import build_change_summary, enrich_delta_items
from src.engines.inventory_reconcile.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationEntityType,
)
from src.inventory.display_lookups import ApplicationDisplay

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    operation: str = 'create',
    entity_type: str = 'access_fact',
    before_json: dict | None = None,
    after_json: dict | None = None,
    effect: str | None = 'allow',
    subject_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
    entity_id: uuid.UUID | None = None,
) -> MagicMock:
    item = MagicMock(spec=ReconciliationDeltaItem)
    item.id = uuid.uuid4()
    item.reconciliation_run_id = uuid.uuid4()
    item.entity_type = ReconciliationEntityType(entity_type)
    item.operation = operation
    item.natural_key_hash = 'a' * 64
    item.subject_id = subject_id or uuid.uuid4()
    item.account_id = account_id
    item.resource_id = resource_id or uuid.uuid4()
    item.action_id = 1
    item.effect = effect
    item.entity_id = entity_id
    item.existing_fact_id = None
    item.source_artifact_id = uuid.uuid4()
    item.before_json = before_json
    item.after_json = after_json
    item.status = ReconciliationDeltaItemStatus.pending
    item.reason = None
    item.created_at = datetime.now(UTC)
    item.applied_at = None
    return item


# ---------------------------------------------------------------------------
# Tests: build_change_summary
# ---------------------------------------------------------------------------


def test_change_summary_noop() -> None:
    result = build_change_summary('noop', None, None, None)
    assert result == 'unchanged'


def test_change_summary_create_with_role() -> None:
    result = build_change_summary('create', None, {'role': 'admin'}, None)
    assert result == '+ admin'


def test_change_summary_create_with_effect_fallback() -> None:
    result = build_change_summary('create', None, {}, 'allow')
    assert result == '+ allow'


def test_change_summary_create_no_info() -> None:
    result = build_change_summary('create', None, {}, None)
    assert result == '+ created'


def test_change_summary_revoke_with_role() -> None:
    result = build_change_summary('revoke', {'role': 'viewer'}, None, None)
    assert result == '- viewer'


def test_change_summary_revoke_fallback() -> None:
    result = build_change_summary('revoke', {}, None, None)
    assert result == '- revoked'


def test_change_summary_update_role_change() -> None:
    result = build_change_summary('update', {'role': 'viewer'}, {'role': 'admin'}, None)
    assert result == 'viewer → admin'


def test_change_summary_update_same_role() -> None:
    # Same role — fall back to key count
    result = build_change_summary('update', {'role': 'admin', 'x': '1'}, {'role': 'admin', 'x': '2'}, None)
    # 'x' changed
    assert '1 field' in result or 'changed' in result


def test_change_summary_reactivate() -> None:
    result = build_change_summary('reactivate', None, {'role': 'member'}, None)
    assert result == '↻ member'


def test_change_summary_reactivate_fallback() -> None:
    result = build_change_summary('reactivate', None, {}, None)
    assert result == '↻ reactivated'


# ---------------------------------------------------------------------------
# Tests: enrich_delta_items (with mocked DB lookups)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_delta_items_resolves_display_fields() -> None:
    """enrich_delta_items populates display fields using batch lookup results."""
    subj_id = uuid.uuid4()
    acc_id = uuid.uuid4()
    res_id = uuid.uuid4()
    app_id = uuid.uuid4()

    item = _make_item(
        operation='create',
        entity_type='access_fact',
        after_json={'role': 'admin'},
        subject_id=subj_id,
        account_id=acc_id,
        resource_id=res_id,
    )

    rows: list[tuple] = [(item, app_id)]

    mock_session = AsyncMock()

    # Patch display_lookups functions used by enrich_delta_items.
    # Subject lookup now goes through batch_display_by_subject_ids (subjects table JOIN).
    with (
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={subj_id: 'Alice Smith'}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={acc_id: 'alice@example.com'}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={res_id: 'aurelion/kernel (repository)'}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={app_id: ApplicationDisplay(code='GHE', name='GitHub Enterprise')}),
        ),
    ):
        result = await enrich_delta_items(mock_session, rows)

    assert len(result) == 1
    item_read = result[0]
    assert item_read.subject_display == 'Alice Smith'
    assert item_read.account_display == 'alice@example.com'
    assert item_read.resource_display == 'aurelion/kernel (repository)'
    assert item_read.application_code == 'GHE'
    assert item_read.application_name == 'GitHub Enterprise'
    assert item_read.change_summary == '+ admin'


@pytest.mark.asyncio
async def test_enrich_delta_items_fallback_none_for_missing() -> None:
    """When lookup maps return empty, display fields are None."""
    item = _make_item(operation='create', entity_type='access_fact')
    rows: list[tuple] = [(item, uuid.uuid4())]

    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_delta_items(mock_session, rows)

    assert result[0].subject_display is None
    assert result[0].account_display is None
    assert result[0].resource_display is None
    assert result[0].application_code is None


@pytest.mark.asyncio
async def test_enrich_delta_items_employee_entity_type() -> None:
    """For entity_type=employee, entity_id is used for subject_display."""
    entity_id = uuid.uuid4()
    item = _make_item(operation='update', entity_type='employee', entity_id=entity_id)
    rows: list[tuple] = [(item, None)]

    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={entity_id: 'Bob Jones'}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_delta_items(mock_session, rows)

    assert result[0].subject_display == 'Bob Jones'


@pytest.mark.asyncio
async def test_enrich_delta_items_nhi_subject() -> None:
    """NHI subject_id is resolved via batch_display_by_subject_ids (subjects JOIN nhis)."""
    subject_id = uuid.uuid4()
    item = _make_item(operation='create', entity_type='access_fact', subject_id=subject_id)
    rows: list[tuple] = [(item, None)]

    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={subject_id: 'svc-bot-01'}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_delta_items(mock_session, rows)

    assert result[0].subject_display == 'svc-bot-01'


# ---------------------------------------------------------------------------
# Tests: account_display fallback chain for entity_type='account'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_account_display_resolved_via_entity_id() -> None:
    """account-delta update: entity_id resolves to username via accounts table."""
    entity_id = uuid.uuid4()
    item = _make_item(
        operation='update',
        entity_type='account',
        entity_id=entity_id,
        account_id=None,
        before_json={'mfa_enabled': True},
        after_json={'mfa_enabled': False},
    )
    rows: list[tuple] = [(item, None)]
    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_account_display',
            # entity_id is included in the batch; returns username for it
            new=AsyncMock(return_value={entity_id: 'alice@corp.com'}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_delta_items(mock_session, rows)

    assert result[0].account_display == 'alice@corp.com'


@pytest.mark.asyncio
async def test_account_display_fallback_after_json_username() -> None:
    """account-delta create: entity_id=None, username taken from after_json."""
    item = _make_item(
        operation='create',
        entity_type='account',
        entity_id=None,
        account_id=None,
        after_json={'username': 'new.hire@company.com', 'status': 'active'},
    )
    rows: list[tuple] = [(item, None)]
    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_delta_items(mock_session, rows)

    assert result[0].account_display == 'new.hire@company.com'


@pytest.mark.asyncio
async def test_account_display_fallback_before_json_username() -> None:
    """account-delta revoke: entity_id not in map, username taken from before_json."""
    item = _make_item(
        operation='revoke',
        entity_type='account',
        entity_id=None,
        account_id=None,
        before_json={'username': 'ex.employee@corp.com'},
        after_json=None,
    )
    rows: list[tuple] = [(item, None)]
    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_delta_items(mock_session, rows)

    assert result[0].account_display == 'ex.employee@corp.com'


@pytest.mark.asyncio
async def test_account_display_none_when_no_info() -> None:
    """account-delta: no account_id, no entity_id, no username in json → None."""
    item = _make_item(
        operation='update',
        entity_type='account',
        entity_id=None,
        account_id=None,
        before_json={'mfa_enabled': True},
        after_json={'mfa_enabled': False},
    )
    rows: list[tuple] = [(item, None)]
    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.inventory_reconcile.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_delta_items(mock_session, rows)

    assert result[0].account_display is None

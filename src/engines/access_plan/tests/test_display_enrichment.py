# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for access_plan display_enrichment module.

Covers:
- build_target_display (pure)
- build_change_summary (pure)
- enrich_plan_items (with mocked DB lookups)
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from src.engines.access_plan.display_enrichment import (
    build_change_summary,
    build_target_display,
    enrich_plan_items,
)
from src.engines.access_plan.schemas import PlanItemRead
from src.inventory.display_lookups import ApplicationDisplay

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_item(
    *,
    kind: str = 'grant_role',
    application: str = 'GHE',
    subject_ref: str | None = None,
    subject_type: str = 'employee',
    target_descriptor: dict | None = None,
) -> PlanItemRead:
    return PlanItemRead(
        id=uuid.uuid4(),
        plan_id=uuid.uuid4(),
        plan_status='active',
        subject_ref=subject_ref or str(uuid.uuid4()),
        subject_type=subject_type,
        kind=kind,
        application=application,
        account_ref=None,
        target_descriptor=target_descriptor or {},
        initiatives=[],
        initiative_refs=[],
        policy_rule_refs=[],
        decision_snapshot={},
        execution_status='proposed',
        failure_reason=None,
        last_verified_at=None,
        last_error=None,
        created_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Tests: build_target_display
# ---------------------------------------------------------------------------


def test_target_display_grant_role() -> None:
    result = build_target_display('grant_role', {'role': 'admin'})
    assert result == 'role: admin'


def test_target_display_revoke_role() -> None:
    result = build_target_display('revoke_role', {'role': 'viewer'})
    assert result == 'role: viewer'


def test_target_display_group_add() -> None:
    result = build_target_display('group_add', {'group': 'engineering'})
    assert result == 'group: engineering'


def test_target_display_entitlement_attach() -> None:
    result = build_target_display('entitlement_attach', {'slug': 'github-write'})
    assert result == 'entitlement: github-write'


def test_target_display_account_create_with_username() -> None:
    result = build_target_display('account_create', {'username': 'jdoe'})
    assert result == 'account: jdoe'


def test_target_display_account_create_no_fields() -> None:
    result = build_target_display('account_create', {})
    assert result == 'account'


def test_target_display_unknown_kind() -> None:
    result = build_target_display('unknown_kind', {'role': 'admin'})
    assert result is None


# ---------------------------------------------------------------------------
# Tests: build_change_summary
# ---------------------------------------------------------------------------


def test_change_summary_grant_role_with_target() -> None:
    result = build_change_summary('grant_role', 'role: admin')
    assert result == '+ role: admin'


def test_change_summary_revoke_role_with_target() -> None:
    result = build_change_summary('revoke_role', 'role: viewer')
    assert result == '- role: viewer'


def test_change_summary_account_create() -> None:
    result = build_change_summary('account_create', None)
    assert result == '+ create account'


def test_change_summary_account_activate() -> None:
    result = build_change_summary('account_activate', None)
    assert result == '↻ activate'


def test_change_summary_account_suspend() -> None:
    result = build_change_summary('account_suspend', None)
    assert result == '⏸ suspend'


def test_change_summary_account_disable() -> None:
    result = build_change_summary('account_disable', None)
    assert result == '⊘ disable'


def test_change_summary_grant_no_target() -> None:
    result = build_change_summary('grant_role', None)
    assert result == '+ grant'


def test_change_summary_revoke_no_target() -> None:
    result = build_change_summary('group_remove', None)
    assert result == '- revoke'


def test_change_summary_unknown_kind() -> None:
    result = build_change_summary('unknown_kind', None)
    assert result is None


# ---------------------------------------------------------------------------
# Tests: enrich_plan_items
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_plan_items_resolves_employee_display() -> None:
    subject_id = uuid.uuid4()
    emp_id = uuid.uuid4()
    item = _make_item(
        kind='grant_role',
        application='GHE',
        subject_ref=str(subject_id),
        target_descriptor={'role': 'admin'},
    )

    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.access_plan.display_enrichment._batch_subject_principal_ids',
            new=AsyncMock(return_value={subject_id: (emp_id, None)}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_employee_display',
            new=AsyncMock(return_value={emp_id: 'Alice Smith'}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_nhi_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_application_display_by_code',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_plan_items(mock_session, [item])

    assert len(result) == 1
    enriched = result[0]
    assert enriched.subject_display == 'Alice Smith'
    assert enriched.target_display == 'role: admin'
    assert enriched.change_summary == '+ role: admin'
    # GHE is not a UUID → application_code = 'GHE'
    assert enriched.application_code == 'GHE'


@pytest.mark.asyncio
async def test_enrich_plan_items_resolves_nhi_display() -> None:
    subject_id = uuid.uuid4()
    nhi_id = uuid.uuid4()
    item = _make_item(
        kind='account_create',
        application='GHE',
        subject_ref=str(subject_id),
        subject_type='nhi',
    )

    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.access_plan.display_enrichment._batch_subject_principal_ids',
            new=AsyncMock(return_value={subject_id: (None, nhi_id)}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_employee_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_nhi_display',
            new=AsyncMock(return_value={nhi_id: 'svc-bot'}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_application_display_by_code',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_plan_items(mock_session, [item])

    assert result[0].subject_display == 'svc-bot'
    assert result[0].change_summary == '+ create account'


@pytest.mark.asyncio
async def test_enrich_plan_items_uuid_application_resolved() -> None:
    subject_id = uuid.uuid4()
    app_id = uuid.uuid4()
    item = _make_item(
        kind='grant_role',
        application=str(app_id),  # UUID string — should be resolved
        subject_ref=str(subject_id),
        target_descriptor={'role': 'member'},
    )

    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.access_plan.display_enrichment._batch_subject_principal_ids',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_employee_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_nhi_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={app_id: ApplicationDisplay(code='JIRA', name='Jira Software')}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_application_display_by_code',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_plan_items(mock_session, [item])

    assert result[0].application_code == 'JIRA'
    assert result[0].application_name == 'Jira Software'


@pytest.mark.asyncio
async def test_enrich_plan_items_fallback_none_for_missing() -> None:
    item = _make_item(kind='grant_role', application='GHE')
    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.access_plan.display_enrichment._batch_subject_principal_ids',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_employee_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_nhi_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_application_display_by_code',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_plan_items(mock_session, [item])

    assert result[0].subject_display is None
    assert result[0].application_code == 'GHE'  # plain string preserved
    assert result[0].application_name is None  # no DB record found


@pytest.mark.asyncio
async def test_enrich_plan_items_code_string_resolved_to_full_name() -> None:
    """PlanItem.application = 'GHE' (short code) → application_name resolved via reverse-lookup."""
    item = _make_item(kind='grant_role', application='GHE')
    mock_session = AsyncMock()

    with (
        patch(
            'src.engines.access_plan.display_enrichment._batch_subject_principal_ids',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_employee_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_nhi_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.engines.access_plan.display_enrichment.batch_application_display_by_code',
            new=AsyncMock(return_value={'GHE': ApplicationDisplay(code='GHE', name='GitHub Enterprise')}),
        ),
    ):
        result = await enrich_plan_items(mock_session, [item])

    enriched = result[0]
    assert enriched.application_code == 'GHE'
    assert enriched.application_name == 'GitHub Enterprise'

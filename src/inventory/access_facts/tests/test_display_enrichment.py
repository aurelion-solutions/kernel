# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for access_facts display_enrichment — list endpoint display fields."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
import uuid

import pytest
from src.inventory.access_facts.display_enrichment import enrich_access_facts
from src.inventory.access_facts.schemas import AccessFactEffect, AccessFactView
from src.inventory.display_lookups import ApplicationDisplay

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_view(
    *,
    subject_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    resource_id: uuid.UUID | None = None,
) -> AccessFactView:
    return AccessFactView(
        id=uuid.uuid4(),
        subject_id=subject_id or uuid.uuid4(),
        account_id=account_id,
        resource_id=resource_id or uuid.uuid4(),
        action_id=1,
        action_slug='read',
        effect=AccessFactEffect.allow,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=None,
        is_active=True,
        revoked_at=None,
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_access_facts_resolves_all_display_fields() -> None:
    """All display fields resolved when lookups return data."""
    subject_id = uuid.uuid4()
    account_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    app_id = uuid.uuid4()

    view = _make_view(subject_id=subject_id, account_id=account_id, resource_id=resource_id)
    mock_session = AsyncMock()

    # Subject lookup now uses batch_display_by_subject_ids (subjects table JOIN).
    with (
        patch(
            'src.inventory.access_facts.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={subject_id: 'Alice Smith'}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={account_id: 'alice@gh.com'}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={resource_id: 'aurelion/kernel (repository)'}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment._batch_resource_application',
            new=AsyncMock(return_value={resource_id: app_id}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={app_id: ApplicationDisplay(code='GHE', name='GitHub Enterprise')}),
        ),
    ):
        result = await enrich_access_facts(mock_session, [view])

    assert len(result) == 1
    r = result[0]
    assert r.subject_display == 'Alice Smith'
    assert r.account_display == 'alice@gh.com'
    assert r.resource_display == 'aurelion/kernel (repository)'
    assert r.application_code == 'GHE'
    assert r.application_name == 'GitHub Enterprise'
    # Original fields preserved
    assert r.id == view.id
    assert r.subject_id == subject_id
    assert r.resource_id == resource_id
    assert r.action_slug == 'read'


@pytest.mark.asyncio
async def test_enrich_access_facts_nhi_subject() -> None:
    """NHI subject resolves via batch_display_by_subject_ids (subjects JOIN nhis)."""
    subject_id = uuid.uuid4()
    resource_id = uuid.uuid4()

    view = _make_view(subject_id=subject_id, resource_id=resource_id)
    mock_session = AsyncMock()

    with (
        patch(
            'src.inventory.access_facts.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={subject_id: 'svc-bot-01'}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment._batch_resource_application',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_access_facts(mock_session, [view])

    assert result[0].subject_display == 'svc-bot-01'


@pytest.mark.asyncio
async def test_enrich_access_facts_fallback_none_for_missing() -> None:
    """All display fields are None when lookups return empty."""
    view = _make_view()
    mock_session = AsyncMock()

    with (
        patch(
            'src.inventory.access_facts.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment._batch_resource_application',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_access_facts(mock_session, [view])

    r = result[0]
    assert r.subject_display is None
    assert r.account_display is None
    assert r.resource_display is None
    assert r.application_code is None


@pytest.mark.asyncio
async def test_enrich_access_facts_no_account_id() -> None:
    """account_display is None when account_id is None on the view."""
    view = _make_view(account_id=None)
    mock_session = AsyncMock()

    with (
        patch(
            'src.inventory.access_facts.display_enrichment.batch_display_by_subject_ids',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_account_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_resource_display',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment._batch_resource_application',
            new=AsyncMock(return_value={}),
        ),
        patch(
            'src.inventory.access_facts.display_enrichment.batch_application_display',
            new=AsyncMock(return_value={}),
        ),
    ):
        result = await enrich_access_facts(mock_session, [view])

    assert result[0].account_display is None

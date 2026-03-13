# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for reconciliation result schemas."""

from src.capabilities.reconciliation.schemas import (
    EntityReconciliationResult,
    ReconciliationResult,
)


def test_entity_reconciliation_result_defaults():
    """EntityReconciliationResult has correct defaults."""
    result = EntityReconciliationResult()
    assert result.source_total == 0
    assert result.created == 0
    assert result.updated == 0
    assert result.unchanged == 0
    assert result.deactivated == 0
    assert result.errors == 0


def test_entity_reconciliation_result_accepts_values():
    """EntityReconciliationResult accepts all counter values."""
    result = EntityReconciliationResult(
        source_total=10,
        created=2,
        updated=3,
        unchanged=4,
        deactivated=1,
        errors=0,
    )
    assert result.source_total == 10
    assert result.created == 2
    assert result.updated == 3
    assert result.unchanged == 4
    assert result.deactivated == 1
    assert result.errors == 0


def test_reconciliation_result_requires_application_id():
    """ReconciliationResult requires application_id."""
    result = ReconciliationResult(application_id='550e8400-e29b-41d4-a716-446655440000')
    assert result.application_id == '550e8400-e29b-41d4-a716-446655440000'
    assert result.accounts.source_total == 0
    assert result.roles.source_total == 0
    assert result.privileges.source_total == 0


def test_reconciliation_result_accepts_entity_results():
    """ReconciliationResult accepts custom entity results."""
    result = ReconciliationResult(
        application_id='app-123',
        accounts=EntityReconciliationResult(source_total=5, created=2, updated=1),
        roles=EntityReconciliationResult(source_total=3, unchanged=3),
        privileges=EntityReconciliationResult(source_total=10, deactivated=1),
    )
    assert result.accounts.source_total == 5
    assert result.accounts.created == 2
    assert result.accounts.updated == 1
    assert result.roles.source_total == 3
    assert result.roles.unchanged == 3
    assert result.privileges.source_total == 10
    assert result.privileges.deactivated == 1

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Reconciliation result schemas."""

from pydantic import BaseModel, Field


class EntityReconciliationResult(BaseModel):
    """Per-entity reconciliation counters. Used for accounts, roles, privileges."""

    source_total: int = Field(0, ge=0, description='Total items in source payload')
    created: int = Field(0, ge=0, description='New records created')
    updated: int = Field(0, ge=0, description='Existing records updated')
    unchanged: int = Field(0, ge=0, description='Records unchanged')
    deactivated: int = Field(0, ge=0, description='Records marked inactive (missing from source)')
    errors: int = Field(0, ge=0, description='Validation or processing errors')


class ReconciliationResult(BaseModel):
    """Top-level reconciliation result for one application."""

    application_id: str = Field(..., description='Application identifier')
    accounts: EntityReconciliationResult = Field(default_factory=EntityReconciliationResult)
    roles: EntityReconciliationResult = Field(default_factory=EntityReconciliationResult)
    privileges: EntityReconciliationResult = Field(default_factory=EntityReconciliationResult)


class ReconciliationAccepted(BaseModel):
    """HTTP 202 body after reconciliation is queued; use ``correlation_id`` with log buffer / events."""

    correlation_id: str = Field(..., description='Trace id for reconciliation.operation_started and follow-up events')
    application_id: str = Field(..., description='Application identifier')

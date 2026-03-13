# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Customer Pydantic schemas."""

from pydantic import ValidationError
import pytest
from src.inventory.customers.schemas import (
    CustomerAttributeCreate,
    CustomerCreate,
    CustomerPatch,
    CustomerPlanTier,
    CustomerTenantRole,
)


def test_customer_create_defaults() -> None:
    """CustomerCreate uses expected defaults."""
    schema = CustomerCreate(external_id='ext-001')
    assert schema.external_id == 'ext-001'
    assert schema.email_verified is False
    assert schema.mfa_enabled is True
    assert schema.is_locked is False
    assert schema.tenant_id is None
    assert schema.tenant_role is None
    assert schema.plan_tier is None
    assert schema.description is None


def test_customer_create_requires_external_id() -> None:
    """CustomerCreate rejects empty external_id."""
    with pytest.raises(ValidationError):
        CustomerCreate(external_id='')


def test_customer_create_with_enums() -> None:
    """CustomerCreate accepts valid enum values."""
    schema = CustomerCreate(
        external_id='ext-002',
        tenant_role=CustomerTenantRole.admin,
        plan_tier=CustomerPlanTier.pro,
    )
    assert schema.tenant_role == CustomerTenantRole.admin
    assert schema.plan_tier == CustomerPlanTier.pro


def test_customer_create_invalid_tenant_role() -> None:
    """CustomerCreate rejects invalid tenant_role."""
    with pytest.raises(ValidationError):
        CustomerCreate(external_id='ext-003', tenant_role='superadmin')  # type: ignore[arg-type]


def test_customer_create_invalid_plan_tier() -> None:
    """CustomerCreate rejects invalid plan_tier."""
    with pytest.raises(ValidationError):
        CustomerCreate(external_id='ext-004', plan_tier='diamond')  # type: ignore[arg-type]


def test_customer_patch_all_optional() -> None:
    """CustomerPatch accepts empty body (all optional)."""
    patch = CustomerPatch()
    assert patch.email_verified is None
    assert patch.mfa_enabled is None
    assert patch.is_locked is None
    assert patch.plan_tier is None


def test_customer_patch_exactly_four_fields() -> None:
    """CustomerPatch has exactly four fields."""
    fields = set(CustomerPatch.model_fields.keys())
    assert fields == {'email_verified', 'mfa_enabled', 'is_locked', 'plan_tier'}


def test_customer_patch_with_plan_tier() -> None:
    """CustomerPatch accepts valid plan_tier."""
    patch = CustomerPatch(plan_tier=CustomerPlanTier.enterprise)
    assert patch.plan_tier == CustomerPlanTier.enterprise


def test_customer_attribute_create_validates_min_length() -> None:
    """CustomerAttributeCreate rejects empty key or value."""
    with pytest.raises(ValidationError):
        CustomerAttributeCreate(key='', value='v')
    with pytest.raises(ValidationError):
        CustomerAttributeCreate(key='k', value='')


def test_tenant_role_enum_values() -> None:
    """CustomerTenantRole has expected values."""
    assert set(CustomerTenantRole) == {
        CustomerTenantRole.admin,
        CustomerTenantRole.member,
        CustomerTenantRole.viewer,
    }


def test_plan_tier_enum_values() -> None:
    """CustomerPlanTier has expected values."""
    assert set(CustomerPlanTier) == {
        CustomerPlanTier.free,
        CustomerPlanTier.basic,
        CustomerPlanTier.pro,
        CustomerPlanTier.enterprise,
    }

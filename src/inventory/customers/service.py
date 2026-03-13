# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Customer service for coordinating repository and log emission."""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.customers.models import Customer, CustomerAttribute, CustomerPlanTier, CustomerTenantRole
from src.inventory.customers.repository import (
    create_customer as repo_create_customer,
)
from src.inventory.customers.repository import (
    create_customer_attribute as repo_create_customer_attribute,
)
from src.inventory.customers.repository import (
    delete_customer_attribute as repo_delete_customer_attribute,
)
from src.inventory.customers.repository import (
    get_customer_by_id as repo_get_customer_by_id,
)
from src.inventory.customers.repository import (
    list_customer_attributes as repo_list_customer_attributes,
)
from src.inventory.customers.repository import (
    list_customers as repo_list_customers,
)
from src.inventory.customers.repository import (
    update_customer as repo_update_customer,
)
from src.inventory.customers.schemas import CustomerPatch
from src.inventory.subjects.models import SubjectKind
from src.inventory.subjects.service import SubjectService
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields, noop_log_service

_COMPONENT = 'inventory.customers'


class CustomerNotFoundError(Exception):
    """Raised when a customer is not found."""

    def __init__(self, customer_id: uuid.UUID) -> None:
        self.customer_id = customer_id
        super().__init__(f'Customer not found: {customer_id}')


class CustomerAttributeNotFoundError(Exception):
    """Raised when a customer attribute is not found."""

    def __init__(self, customer_id: uuid.UUID, key: str) -> None:
        self.customer_id = customer_id
        self.key = key
        super().__init__(f'Customer attribute not found: {customer_id} / {key}')


class DuplicateCustomerAttributeError(Exception):
    """Raised when adding an attribute with a key that already exists for the customer."""

    def __init__(self, customer_id: uuid.UUID, key: str) -> None:
        self.customer_id = customer_id
        self.key = key
        super().__init__(f'Duplicate attribute key for customer: {key}')


class CustomerService:
    """Orchestrates customer CRUD and log emission."""

    def __init__(
        self,
        log_service: LogService | None = None,
        subject_service: SubjectService | None = None,
    ) -> None:
        self._log = log_service if log_service is not None else noop_log_service
        self._subject_service = (
            subject_service if subject_service is not None else SubjectService(log_service=log_service)
        )

    async def create_customer(
        self,
        session: AsyncSession,
        *,
        external_id: str,
        email_verified: bool = False,
        tenant_id: str | None = None,
        tenant_role: CustomerTenantRole | None = None,
        plan_tier: CustomerPlanTier | None = None,
        mfa_enabled: bool = True,
        is_locked: bool = False,
        description: str | None = None,
    ) -> Customer:
        """Create a customer and emit customer.created."""
        customer = await repo_create_customer(
            session,
            external_id=external_id,
            email_verified=email_verified,
            tenant_id=tenant_id,
            tenant_role=tenant_role,
            plan_tier=plan_tier,
            mfa_enabled=mfa_enabled,
            is_locked=is_locked,
            description=description,
        )
        payload: dict[str, object] = {
            'customer_id': str(customer.id),
            'external_id': customer.external_id,
            'tenant_id': customer.tenant_id,
            'plan_tier': customer.plan_tier.value if customer.plan_tier else None,
        }
        self._log.emit_safe(
            'customer.created',
            LogLevel.INFO,
            'Customer created',
            _COMPONENT,
            merge_emit_log_participant_fields(
                payload,
                actor_component=_COMPONENT,
                target_id='customer',
            ),
        )
        return customer

    async def get_customer(
        self,
        session: AsyncSession,
        customer_id: uuid.UUID,
    ) -> Customer | None:
        """Get customer by id. Emits customer.retrieved when found."""
        customer = await repo_get_customer_by_id(session, customer_id)
        if customer is not None:
            self._log.emit_safe(
                'customer.retrieved',
                LogLevel.INFO,
                'Customer retrieved',
                _COMPONENT,
                merge_emit_log_participant_fields(
                    {'customer_id': str(customer_id)},
                    actor_component=_COMPONENT,
                    target_id='customer',
                ),
            )
        return customer

    async def list_customers(
        self,
        session: AsyncSession,
        *,
        plan_tier: CustomerPlanTier | None = None,
        is_locked: bool | None = None,
    ) -> list[Customer]:
        """List customers. No event emitted."""
        return await repo_list_customers(session, plan_tier=plan_tier, is_locked=is_locked)

    async def update_customer(
        self,
        session: AsyncSession,
        customer_id: uuid.UUID,
        patch: CustomerPatch,
    ) -> Customer:
        """Apply partial update and emit customer.updated if fields changed."""
        customer = await repo_get_customer_by_id(session, customer_id)
        if customer is None:
            raise CustomerNotFoundError(customer_id)

        changed_fields = await repo_update_customer(
            session,
            customer,
            email_verified=patch.email_verified,
            mfa_enabled=patch.mfa_enabled,
            is_locked=patch.is_locked,
            plan_tier=patch.plan_tier,
        )
        if changed_fields:
            self._log.emit_safe(
                'customer.updated',
                LogLevel.INFO,
                'Customer updated',
                _COMPONENT,
                merge_emit_log_participant_fields(
                    {
                        'customer_id': str(customer_id),
                        'changed_fields': sorted(changed_fields),
                    },
                    actor_component=_COMPONENT,
                    target_id='customer',
                ),
            )
            if changed_fields & {'is_locked', 'email_verified'}:
                await self._subject_service.recompute_status_for_principal(
                    session,
                    kind=SubjectKind.customer,
                    principal_id=customer_id,
                )
        return customer

    async def list_attributes(
        self,
        session: AsyncSession,
        customer_id: uuid.UUID,
    ) -> list[CustomerAttribute]:
        """List attributes for a customer. Raises CustomerNotFoundError if missing."""
        customer = await repo_get_customer_by_id(session, customer_id)
        if customer is None:
            raise CustomerNotFoundError(customer_id)
        return await repo_list_customer_attributes(session, customer_id)

    async def add_attribute(
        self,
        session: AsyncSession,
        customer_id: uuid.UUID,
        key: str,
        value: str,
    ) -> CustomerAttribute:
        """Add attribute to customer. Emits customer.attribute.added. Raises on duplicate."""
        customer = await repo_get_customer_by_id(session, customer_id)
        if customer is None:
            raise CustomerNotFoundError(customer_id)
        try:
            attr = await repo_create_customer_attribute(
                session,
                customer_id=customer_id,
                key=key,
                value=value,
            )
        except IntegrityError:
            raise DuplicateCustomerAttributeError(customer_id, key) from None
        self._log.emit_safe(
            'customer.attribute.added',
            LogLevel.INFO,
            'Customer attribute added',
            _COMPONENT,
            merge_emit_log_participant_fields(
                {'customer_id': str(customer_id), 'key': key},
                actor_component=_COMPONENT,
                target_id='customer',
            ),
        )
        return attr

    async def remove_attribute(
        self,
        session: AsyncSession,
        customer_id: uuid.UUID,
        key: str,
    ) -> None:
        """Remove attribute from customer. Emits customer.attribute.removed. Raises if missing."""
        customer = await repo_get_customer_by_id(session, customer_id)
        if customer is None:
            raise CustomerNotFoundError(customer_id)
        deleted = await repo_delete_customer_attribute(session, customer_id, key)
        if not deleted:
            raise CustomerAttributeNotFoundError(customer_id, key)
        self._log.emit_safe(
            'customer.attribute.removed',
            LogLevel.INFO,
            'Customer attribute removed',
            _COMPONENT,
            merge_emit_log_participant_fields(
                {'customer_id': str(customer_id), 'key': key},
                actor_component=_COMPONENT,
                target_id='customer',
            ),
        )

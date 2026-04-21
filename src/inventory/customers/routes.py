# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Customer API routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.inventory.customers.deps import get_customer_service
from src.inventory.customers.schemas import (
    CustomerAttributeCreate,
    CustomerAttributeRead,
    CustomerCreate,
    CustomerPatch,
    CustomerPlanTier,
    CustomerRead,
)
from src.inventory.customers.service import (
    CustomerAttributeNotFoundError,
    CustomerNotFoundError,
    CustomerService,
    DuplicateCustomerAttributeError,
)

router = APIRouter(prefix='/customers', tags=['customers'])
DependsSession = Depends(get_db)
DependsService = Depends(get_customer_service)


@router.post('', response_model=CustomerRead, status_code=201)
async def create_customer(
    body: CustomerCreate,
    session: AsyncSession = DependsSession,
    service: CustomerService = DependsService,
) -> CustomerRead:
    """Create a customer."""
    customer = await service.create_customer(
        session,
        external_id=body.external_id,
        email_verified=body.email_verified,
        tenant_id=body.tenant_id,
        tenant_role=body.tenant_role,
        plan_tier=body.plan_tier,
        mfa_enabled=body.mfa_enabled,
        is_locked=body.is_locked,
        description=body.description,
    )
    await session.commit()
    return CustomerRead.model_validate(customer)


@router.get('', response_model=list[CustomerRead])
async def list_customers(
    plan_tier: CustomerPlanTier | None = None,
    is_locked: bool | None = None,
    session: AsyncSession = DependsSession,
    service: CustomerService = DependsService,
) -> list[CustomerRead]:
    """List customers with optional filters."""
    customers = await service.list_customers(
        session,
        plan_tier=plan_tier,
        is_locked=is_locked,
    )
    return [CustomerRead.model_validate(c) for c in customers]


@router.get('/{customer_id}', response_model=CustomerRead)
async def get_customer(
    customer_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: CustomerService = DependsService,
) -> CustomerRead:
    """Get customer by id."""
    customer = await service.get_customer(session, customer_id)
    if customer is None:
        raise HTTPException(status_code=404, detail='Customer not found')
    return CustomerRead.model_validate(customer)


@router.patch('/{customer_id}', response_model=CustomerRead)
async def update_customer(
    customer_id: uuid.UUID,
    body: CustomerPatch,
    session: AsyncSession = DependsSession,
    service: CustomerService = DependsService,
) -> CustomerRead:
    """Partially update a customer."""
    try:
        customer = await service.update_customer(session, customer_id, body)
    except CustomerNotFoundError:
        raise HTTPException(status_code=404, detail='Customer not found') from None
    await session.commit()
    return CustomerRead.model_validate(customer)


@router.get('/{customer_id}/attributes', response_model=list[CustomerAttributeRead])
async def list_customer_attributes(
    customer_id: uuid.UUID,
    session: AsyncSession = DependsSession,
    service: CustomerService = DependsService,
) -> list[CustomerAttributeRead]:
    """List attributes for a customer."""
    try:
        attrs = await service.list_attributes(session, customer_id)
    except CustomerNotFoundError:
        raise HTTPException(status_code=404, detail='Customer not found') from None
    return [CustomerAttributeRead.model_validate(a) for a in attrs]


@router.post(
    '/{customer_id}/attributes',
    response_model=CustomerAttributeRead,
    status_code=201,
)
async def add_customer_attribute(
    customer_id: uuid.UUID,
    body: CustomerAttributeCreate,
    session: AsyncSession = DependsSession,
    service: CustomerService = DependsService,
) -> CustomerAttributeRead:
    """Add attribute to a customer."""
    try:
        attr = await service.add_attribute(
            session,
            customer_id=customer_id,
            key=body.key,
            value=body.value,
        )
    except CustomerNotFoundError:
        raise HTTPException(status_code=404, detail='Customer not found') from None
    except DuplicateCustomerAttributeError:
        raise HTTPException(
            status_code=409,
            detail=f'Attribute key already exists for this customer: {body.key}',
        ) from None
    await session.commit()
    return CustomerAttributeRead.model_validate(attr)


@router.delete('/{customer_id}/attributes/{key}', status_code=204)
async def remove_customer_attribute(
    customer_id: uuid.UUID,
    key: str,
    session: AsyncSession = DependsSession,
    service: CustomerService = DependsService,
) -> None:
    """Remove attribute from a customer."""
    try:
        await service.remove_attribute(session, customer_id, key)
    except CustomerNotFoundError:
        raise HTTPException(status_code=404, detail='Customer not found') from None
    except CustomerAttributeNotFoundError:
        raise HTTPException(status_code=404, detail='Customer attribute not found') from None
    await session.commit()

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Model tests for the Capability ORM."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from src.capabilities.access_analysis.capabilities.models import Capability


@pytest.mark.asyncio
async def test_create_capability_persists_all_fields(session_factory) -> None:
    """Inserting a Capability persists all columns including defaults."""
    async with session_factory() as session:
        cap = Capability(
            slug='approve_payment',
            name='Approve Payment',
            description='Approve a payment transaction.',
            is_active=True,
            created_by='alice@example.com',
        )
        session.add(cap)
        await session.flush()
        cap_id = cap.id
        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(Capability, cap_id)
        assert fetched is not None
        assert fetched.slug == 'approve_payment'
        assert fetched.name == 'Approve Payment'
        assert fetched.description == 'Approve a payment transaction.'
        assert fetched.is_active is True
        assert fetched.created_by == 'alice@example.com'
        assert fetched.created_at is not None
        assert fetched.id == cap_id


@pytest.mark.asyncio
async def test_capability_slug_is_unique(session_factory) -> None:
    """Inserting two capabilities with the same slug raises IntegrityError."""
    async with session_factory() as session:
        cap1 = Capability(slug='create_vendor', name='Create Vendor')
        session.add(cap1)
        await session.flush()

        cap2 = Capability(slug='create_vendor', name='Create Vendor Duplicate')
        session.add(cap2)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_capability_description_and_created_by_nullable(session_factory) -> None:
    """Capability with description=None and created_by=None inserts successfully."""
    async with session_factory() as session:
        cap = Capability(
            slug='post_journal_entry',
            name='Post Journal Entry',
            description=None,
            created_by=None,
        )
        session.add(cap)
        await session.flush()
        cap_id = cap.id
        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(Capability, cap_id)
        assert fetched is not None
        assert fetched.description is None
        assert fetched.created_by is None
        assert fetched.is_active is True  # server default


@pytest.mark.asyncio
async def test_capability_is_active_defaults_to_true(session_factory) -> None:
    """is_active defaults to True when not explicitly provided."""
    async with session_factory() as session:
        cap = Capability(slug='view_report', name='View Report')
        session.add(cap)
        await session.flush()
        assert cap.is_active is True


@pytest.mark.asyncio
async def test_capability_can_be_deactivated(session_factory) -> None:
    """is_active can be explicitly set to False."""
    async with session_factory() as session:
        cap = Capability(slug='delete_record', name='Delete Record', is_active=False)
        session.add(cap)
        await session.flush()
        cap_id = cap.id
        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(Capability, cap_id)
        assert fetched is not None
        assert fetched.is_active is False


@pytest.mark.asyncio
async def test_capability_session_refresh_after_flush(session_factory) -> None:
    """After flush and refresh, all server-default columns are populated."""
    async with session_factory() as session:
        cap = Capability(slug='manage_access', name='Manage Access')
        session.add(cap)
        await session.flush()
        await session.refresh(cap)
        assert cap.id is not None
        assert cap.created_at is not None

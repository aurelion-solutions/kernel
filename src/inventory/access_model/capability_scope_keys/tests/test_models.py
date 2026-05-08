# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Model tests for the CapabilityScopeKey ORM."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.access_model.capability_scope_keys.models import CapabilityScopeKey


@pytest.mark.asyncio
async def test_create_scope_key_persists_all_fields(session_factory) -> None:
    """Inserting a CapabilityScopeKey persists all columns including defaults."""
    async with session_factory() as session:
        sk = CapabilityScopeKey(
            code='LEGAL_ENTITY',
            name='Legal entity',
            description='Scope bounded by a legal entity boundary.',
            is_active=True,
            created_by='alice@example.com',
        )
        session.add(sk)
        await session.flush()
        sk_id = sk.id
        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(CapabilityScopeKey, sk_id)
        assert fetched is not None
        assert fetched.code == 'LEGAL_ENTITY'
        assert fetched.name == 'Legal entity'
        assert fetched.description == 'Scope bounded by a legal entity boundary.'
        assert fetched.is_active is True
        assert fetched.created_by == 'alice@example.com'
        assert fetched.created_at is not None
        assert fetched.id == sk_id


@pytest.mark.asyncio
async def test_scope_key_code_is_unique(session_factory) -> None:
    """Inserting two scope keys with the same code raises IntegrityError."""
    async with session_factory() as session:
        sk1 = CapabilityScopeKey(code='GLOBAL', name='Global')
        session.add(sk1)
        await session.flush()

        sk2 = CapabilityScopeKey(code='GLOBAL', name='Global Duplicate')
        session.add(sk2)
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_scope_key_description_and_created_by_nullable(session_factory) -> None:
    """CapabilityScopeKey with description=None and created_by=None inserts successfully."""
    async with session_factory() as session:
        sk = CapabilityScopeKey(
            code='PROJECT',
            name='Project',
            description=None,
            created_by=None,
        )
        session.add(sk)
        await session.flush()
        sk_id = sk.id
        await session.commit()

    async with session_factory() as session:
        fetched = await session.get(CapabilityScopeKey, sk_id)
        assert fetched is not None
        assert fetched.description is None
        assert fetched.created_by is None
        assert fetched.is_active is True  # server default

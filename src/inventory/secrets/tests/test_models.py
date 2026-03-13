# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for Secret model."""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError
from src.inventory.secrets.models import Secret


@pytest.mark.asyncio
async def test_secret_instantiation_with_required_fields(session_factory) -> None:
    """Secret model can be instantiated with required fields."""
    async with session_factory() as session:
        secret = Secret(key='my-key', provider='file', namespace='default')
        session.add(secret)
        await session.commit()
        assert secret.key == 'my-key'
        assert secret.provider == 'file'
        assert secret.namespace == 'default'
        assert secret.created_at is not None
        assert secret.updated_at is not None


@pytest.mark.asyncio
async def test_secret_id_is_uuid_primary_key(session_factory) -> None:
    """Secret id is UUID primary key."""
    async with session_factory() as session:
        secret = Secret(key='test-key', provider='file', namespace='default')
        session.add(secret)
        await session.commit()
        assert isinstance(secret.id, uuid.UUID)
        assert secret.id is not None


@pytest.mark.asyncio
async def test_secret_key_provider_namespace_uniqueness(session_factory) -> None:
    """key + provider + namespace must be unique."""
    async with session_factory() as session:
        s1 = Secret(key='dup', provider='file', namespace='default')
        session.add(s1)
        await session.commit()

    async with session_factory() as session:
        s2 = Secret(key='dup', provider='file', namespace='default')
        session.add(s2)
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

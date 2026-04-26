# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Model tests for LLMModel and LLMExecutionProfile ORM."""

from __future__ import annotations

from typing import Any
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from src.inventory.secrets.models import Secret
from src.platform.llm.models import LLMExecutionProfile, LLMModel, LLMProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_secret(session: Any) -> Secret:
    secret = Secret(
        key=f'test-key-{uuid.uuid4().hex[:8]}',
        provider='vault',
        namespace='default',
    )
    session.add(secret)
    await session.flush()
    await session.refresh(secret)
    return secret


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_minimal_llama_cpp_model(session_factory: Any) -> None:
    """Insert with only name + provider + local_path; defaults applied."""
    async with session_factory() as session:
        model = LLMModel(
            name=f'test-llama-{uuid.uuid4().hex[:8]}',
            provider=LLMProvider.llama_cpp,
            local_path='/tmp/x.gguf',
        )
        session.add(model)
        await session.flush()
        await session.refresh(model)

        assert model.id is not None
        assert model.provider == LLMProvider.llama_cpp
        assert model.local_path == '/tmp/x.gguf'
        assert model.is_active is True
        assert model.default_params == {}
        assert model.created_at is not None
        assert model.updated_at is not None


@pytest.mark.asyncio
async def test_insert_openai_model_with_secret_fk(session_factory: Any) -> None:
    """Insert a Secret then an LLMModel referencing it; FK resolves on read."""
    async with session_factory() as session:
        secret = await _insert_secret(session)

        model = LLMModel(
            name=f'test-openai-{uuid.uuid4().hex[:8]}',
            provider=LLMProvider.openai,
            endpoint_url='https://api.openai.com/v1',
            model_ref='gpt-4o',
            secret_id=secret.id,
        )
        session.add(model)
        await session.flush()
        await session.refresh(model)

        assert model.secret_id == secret.id
        assert model.provider == LLMProvider.openai


@pytest.mark.asyncio
async def test_unique_name_constraint(session_factory: Any) -> None:
    """Two rows with the same name → IntegrityError on flush of the second."""
    name = f'duplicate-model-{uuid.uuid4().hex[:8]}'
    async with session_factory() as session:
        m1 = LLMModel(name=name, provider=LLMProvider.ollama)
        session.add(m1)
        await session.flush()

        m2 = LLMModel(name=name, provider=LLMProvider.openai)
        session.add(m2)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_provider_enum_values_persist(session_factory: Any) -> None:
    """Insert one row per provider; read back and assert each is the matching member."""
    async with session_factory() as session:
        for provider in LLMProvider:
            model = LLMModel(
                name=f'enum-test-{provider.value}-{uuid.uuid4().hex[:6]}',
                provider=provider,
            )
            session.add(model)
            await session.flush()
            await session.refresh(model)
            assert model.provider == provider


@pytest.mark.asyncio
async def test_secret_fk_restrict_on_delete(session_factory: Any) -> None:
    """Delete a Secret referenced by an LLMModel → IntegrityError (ON DELETE RESTRICT)."""
    async with session_factory() as session:
        secret = await _insert_secret(session)
        secret_id = secret.id
        model = LLMModel(
            name=f'fk-restrict-{uuid.uuid4().hex[:8]}',
            provider=LLMProvider.openai,
            secret_id=secret_id,
        )
        session.add(model)
        await session.flush()
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text('DELETE FROM secrets WHERE id = :id'),
                {'id': str(secret_id)},
            )
            await session.flush()
        await session.rollback()


# ---------------------------------------------------------------------------
# LLMExecutionProfile tests
# ---------------------------------------------------------------------------


async def _insert_llm_model(session: Any) -> LLMModel:
    model = LLMModel(
        name=f'profile-test-model-{uuid.uuid4().hex[:8]}',
        provider=LLMProvider.llama_cpp,
        local_path='/tmp/test.gguf',
    )
    session.add(model)
    await session.flush()
    await session.refresh(model)
    return model


@pytest.mark.asyncio
async def test_insert_minimal_execution_profile(session_factory: Any) -> None:
    """Insert profile with only name + model_id; defaults applied."""
    async with session_factory() as session:
        model = await _insert_llm_model(session)

        profile = LLMExecutionProfile(
            name=f'test-profile-{uuid.uuid4().hex[:8]}',
            model_id=model.id,
        )
        session.add(profile)
        await session.flush()
        await session.refresh(profile)

        assert profile.id is not None
        assert profile.model_id == model.id
        assert profile.param_overrides == {}
        assert profile.created_at is not None
        assert profile.updated_at is not None


@pytest.mark.asyncio
async def test_execution_profile_unique_name_constraint(session_factory: Any) -> None:
    """Two profiles with the same name → IntegrityError on flush of the second."""
    name = f'duplicate-profile-{uuid.uuid4().hex[:8]}'
    async with session_factory() as session:
        model = await _insert_llm_model(session)

        p1 = LLMExecutionProfile(name=name, model_id=model.id)
        session.add(p1)
        await session.flush()

        p2 = LLMExecutionProfile(name=name, model_id=model.id)
        session.add(p2)
        with pytest.raises(IntegrityError):
            await session.flush()
        await session.rollback()


@pytest.mark.asyncio
async def test_execution_profile_param_overrides_persists(session_factory: Any) -> None:
    """param_overrides dict round-trips correctly through JSONB."""
    overrides = {'temperature': 0.0, 'top_p': 1.0, 'max_tokens': 256}
    async with session_factory() as session:
        model = await _insert_llm_model(session)

        profile = LLMExecutionProfile(
            name=f'params-profile-{uuid.uuid4().hex[:8]}',
            model_id=model.id,
            param_overrides=overrides,
        )
        session.add(profile)
        await session.flush()
        await session.refresh(profile)

        assert profile.param_overrides == overrides


@pytest.mark.asyncio
async def test_execution_profile_model_fk_restrict_on_delete(session_factory: Any) -> None:
    """Delete LLMModel referenced by a profile → IntegrityError (ON DELETE RESTRICT)."""
    async with session_factory() as session:
        model = await _insert_llm_model(session)
        model_id = model.id
        profile = LLMExecutionProfile(
            name=f'fk-restrict-profile-{uuid.uuid4().hex[:8]}',
            model_id=model_id,
        )
        session.add(profile)
        await session.flush()
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(IntegrityError):
            await session.execute(
                sa.text('DELETE FROM llm_models WHERE id = :id'),
                {'id': str(model_id)},
            )
            await session.flush()
        await session.rollback()

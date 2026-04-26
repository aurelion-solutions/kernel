# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer tests for LLMModel CRUD.

All tests use the real async DB session fixture from src/conftest.py.
Factory is always a fake (AsyncMock-based) to avoid provider lifecycle.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
import uuid

import pytest
from src.inventory.secrets.models import Secret
from src.platform.llm.exceptions import (
    LLMModelInvalidConfigError,
    LLMModelLocalPathUnreadableError,
    LLMModelNameAlreadyExistsError,
    LLMModelNotFoundError,
)
from src.platform.llm.models import LLMExecutionProfile, LLMProvider
from src.platform.llm.repository import get_by_id
from src.platform.llm.schemas import LLMModelCreate, LLMModelUpdate
from src.platform.llm.service import (
    create_llm_model,
    delete_llm_model,
    update_llm_model,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_fake_factory() -> Any:
    factory = AsyncMock()
    factory.invalidate = AsyncMock()
    return factory


async def _insert_secret(session: Any) -> Secret:
    secret = Secret(
        key=f'svc-test-key-{uuid.uuid4().hex[:8]}',
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
async def test_create_llama_cpp_minimal(session_factory: Any, tmp_path: Any) -> None:
    """Create llama_cpp model with a real temp file; row created with defaults."""
    gguf = tmp_path / 'model.gguf'
    gguf.write_bytes(b'')

    async with session_factory() as session:
        model = await create_llm_model(
            session,
            LLMModelCreate(
                name=f'llama-{uuid.uuid4().hex[:8]}',
                provider=LLMProvider.llama_cpp,
                local_path=str(gguf),
            ),
        )
        assert model.id is not None
        assert model.provider == LLMProvider.llama_cpp
        assert model.is_active is True
        assert model.default_params == {}
        await session.rollback()


@pytest.mark.asyncio
async def test_create_openai_requires_secret_endpoint_model_ref(session_factory: Any) -> None:
    """openai without secret_id raises LLMModelInvalidConfigError."""
    async with session_factory() as session:
        with pytest.raises(LLMModelInvalidConfigError):
            await create_llm_model(
                session,
                LLMModelCreate(
                    name=f'oai-{uuid.uuid4().hex[:8]}',
                    provider=LLMProvider.openai,
                    endpoint_url='https://api.openai.com/v1',
                    model_ref='gpt-4o',
                    # secret_id intentionally omitted
                ),
            )


@pytest.mark.asyncio
async def test_create_ollama_rejects_local_path(session_factory: Any, tmp_path: Any) -> None:
    """ollama with local_path set raises LLMModelInvalidConfigError."""
    gguf = tmp_path / 'model.gguf'
    gguf.write_bytes(b'')

    async with session_factory() as session:
        with pytest.raises(LLMModelInvalidConfigError):
            await create_llm_model(
                session,
                LLMModelCreate(
                    name=f'ollama-{uuid.uuid4().hex[:8]}',
                    provider=LLMProvider.ollama,
                    endpoint_url='http://localhost:11434',
                    model_ref='llama3',
                    local_path=str(gguf),
                ),
            )


@pytest.mark.asyncio
async def test_create_rejects_unreadable_local_path(session_factory: Any) -> None:
    """llama_cpp with non-existent path raises LLMModelLocalPathUnreadableError."""
    async with session_factory() as session:
        with pytest.raises(LLMModelLocalPathUnreadableError):
            await create_llm_model(
                session,
                LLMModelCreate(
                    name=f'llama-{uuid.uuid4().hex[:8]}',
                    provider=LLMProvider.llama_cpp,
                    local_path='/nonexistent/path/model.gguf',
                ),
            )


@pytest.mark.asyncio
async def test_create_rejects_max_total_tokens_exceeding_context_window(session_factory: Any) -> None:
    """max_total_tokens > context_window raises LLMModelInvalidConfigError."""
    async with session_factory() as session:
        with pytest.raises(LLMModelInvalidConfigError, match='max_total_tokens'):
            await create_llm_model(
                session,
                LLMModelCreate(
                    name=f'ollama-{uuid.uuid4().hex[:8]}',
                    provider=LLMProvider.ollama,
                    endpoint_url='http://localhost:11434',
                    model_ref='llama3',
                    context_window=4096,
                    max_total_tokens=8192,
                ),
            )


@pytest.mark.asyncio
async def test_create_duplicate_name_raises_already_exists(session_factory: Any, tmp_path: Any) -> None:
    """Second create with same name raises LLMModelNameAlreadyExistsError."""
    gguf = tmp_path / 'dup.gguf'
    gguf.write_bytes(b'')
    name = f'dup-model-{uuid.uuid4().hex[:8]}'

    async with session_factory() as session:
        await create_llm_model(
            session,
            LLMModelCreate(
                name=name,
                provider=LLMProvider.llama_cpp,
                local_path=str(gguf),
            ),
        )
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(LLMModelNameAlreadyExistsError):
            await create_llm_model(
                session,
                LLMModelCreate(
                    name=name,
                    provider=LLMProvider.llama_cpp,
                    local_path=str(gguf),
                ),
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_update_partial_changes_only_set_fields(session_factory: Any, tmp_path: Any) -> None:
    """PATCH name only — other fields untouched."""
    gguf = tmp_path / 'partial.gguf'
    gguf.write_bytes(b'')
    factory = make_fake_factory()

    async with session_factory() as session:
        model = await create_llm_model(
            session,
            LLMModelCreate(
                name=f'orig-{uuid.uuid4().hex[:8]}',
                provider=LLMProvider.llama_cpp,
                local_path=str(gguf),
                description='original description',
            ),
        )
        model_id = model.id
        await session.commit()

    async with session_factory() as session:
        new_name = f'updated-{uuid.uuid4().hex[:8]}'
        updated = await update_llm_model(
            session,
            model_id,
            LLMModelUpdate(name=new_name),
            factory=factory,
        )
        assert updated.name == new_name
        assert updated.description == 'original description'
        await session.commit()


@pytest.mark.asyncio
async def test_update_clears_nullable_field(session_factory: Any, tmp_path: Any) -> None:
    """PATCH description=None (set, not unset) → DB column becomes NULL."""
    gguf = tmp_path / 'clear.gguf'
    gguf.write_bytes(b'')
    factory = make_fake_factory()

    async with session_factory() as session:
        model = await create_llm_model(
            session,
            LLMModelCreate(
                name=f'clear-{uuid.uuid4().hex[:8]}',
                provider=LLMProvider.llama_cpp,
                local_path=str(gguf),
                description='to be cleared',
            ),
        )
        model_id = model.id
        await session.commit()

    async with session_factory() as session:
        # Construct update with description explicitly set to None
        update = LLMModelUpdate.model_validate({'description': None})
        updated = await update_llm_model(session, model_id, update, factory=factory)
        assert updated.description is None
        await session.commit()


@pytest.mark.asyncio
async def test_update_invalidates_factory_on_deactivate(session_factory: Any, tmp_path: Any) -> None:
    """is_active True → False triggers factory.invalidate."""
    gguf = tmp_path / 'deact.gguf'
    gguf.write_bytes(b'')
    factory = make_fake_factory()

    async with session_factory() as session:
        model = await create_llm_model(
            session,
            LLMModelCreate(
                name=f'deact-{uuid.uuid4().hex[:8]}',
                provider=LLMProvider.llama_cpp,
                local_path=str(gguf),
                is_active=True,
            ),
        )
        model_id = model.id
        await session.commit()

    async with session_factory() as session:
        await update_llm_model(
            session,
            model_id,
            LLMModelUpdate(is_active=False),
            factory=factory,
        )
        factory.invalidate.assert_awaited_once_with(model_id)
        await session.rollback()


@pytest.mark.asyncio
async def test_update_invalidates_factory_on_local_path_change(session_factory: Any, tmp_path: Any) -> None:
    """Changing local_path triggers factory.invalidate."""
    gguf1 = tmp_path / 'path1.gguf'
    gguf2 = tmp_path / 'path2.gguf'
    gguf1.write_bytes(b'')
    gguf2.write_bytes(b'')
    factory = make_fake_factory()

    async with session_factory() as session:
        model = await create_llm_model(
            session,
            LLMModelCreate(
                name=f'path-change-{uuid.uuid4().hex[:8]}',
                provider=LLMProvider.llama_cpp,
                local_path=str(gguf1),
            ),
        )
        model_id = model.id
        await session.commit()

    async with session_factory() as session:
        await update_llm_model(
            session,
            model_id,
            LLMModelUpdate(local_path=str(gguf2)),
            factory=factory,
        )
        factory.invalidate.assert_awaited_once_with(model_id)
        await session.rollback()


@pytest.mark.asyncio
async def test_update_does_not_invalidate_when_only_default_params_change(session_factory: Any, tmp_path: Any) -> None:
    """PATCH default_params only — factory.invalidate NOT called."""
    gguf = tmp_path / 'params.gguf'
    gguf.write_bytes(b'')
    factory = make_fake_factory()

    async with session_factory() as session:
        model = await create_llm_model(
            session,
            LLMModelCreate(
                name=f'params-{uuid.uuid4().hex[:8]}',
                provider=LLMProvider.llama_cpp,
                local_path=str(gguf),
            ),
        )
        model_id = model.id
        await session.commit()

    async with session_factory() as session:
        await update_llm_model(
            session,
            model_id,
            LLMModelUpdate(default_params={'temperature': 0.7}),
            factory=factory,
        )
        factory.invalidate.assert_not_awaited()
        await session.rollback()


@pytest.mark.asyncio
async def test_update_unknown_id_raises_not_found(session_factory: Any) -> None:
    """Update with random UUID raises LLMModelNotFoundError."""
    factory = make_fake_factory()
    async with session_factory() as session:
        with pytest.raises(LLMModelNotFoundError):
            await update_llm_model(
                session,
                uuid.uuid4(),
                LLMModelUpdate(description='x'),
                factory=factory,
            )


@pytest.mark.asyncio
async def test_delete_calls_invalidate(session_factory: Any, tmp_path: Any) -> None:
    """Delete calls factory.invalidate; subsequent get_by_id returns None."""
    gguf = tmp_path / 'del.gguf'
    gguf.write_bytes(b'')
    factory = make_fake_factory()

    async with session_factory() as session:
        model = await create_llm_model(
            session,
            LLMModelCreate(
                name=f'delete-me-{uuid.uuid4().hex[:8]}',
                provider=LLMProvider.llama_cpp,
                local_path=str(gguf),
            ),
        )
        model_id = model.id
        await session.commit()

    async with session_factory() as session:
        await delete_llm_model(session, model_id, factory=factory)
        factory.invalidate.assert_awaited_once_with(model_id)
        await session.commit()

    async with session_factory() as session:
        assert await get_by_id(session, model_id) is None


@pytest.mark.asyncio
async def test_delete_unknown_id_raises_not_found(session_factory: Any) -> None:
    """Delete with random UUID raises LLMModelNotFoundError."""
    factory = make_fake_factory()
    async with session_factory() as session:
        with pytest.raises(LLMModelNotFoundError):
            await delete_llm_model(session, uuid.uuid4(), factory=factory)


@pytest.mark.asyncio
async def test_delete_with_dependent_profile_raises_invalid_config(session_factory: Any, tmp_path: Any) -> None:
    """Model with a dependent profile → LLMModelInvalidConfigError on delete."""
    gguf = tmp_path / 'prof.gguf'
    gguf.write_bytes(b'')
    factory = make_fake_factory()

    async with session_factory() as session:
        model = await create_llm_model(
            session,
            LLMModelCreate(
                name=f'profiled-{uuid.uuid4().hex[:8]}',
                provider=LLMProvider.llama_cpp,
                local_path=str(gguf),
            ),
        )
        model_id = model.id
        # Insert profile manually via ORM — no service call (Step 9 owns that)
        profile = LLMExecutionProfile(
            name=f'profile-{uuid.uuid4().hex[:8]}',
            model_id=model_id,
        )
        session.add(profile)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(LLMModelInvalidConfigError, match='dependent'):
            await delete_llm_model(session, model_id, factory=factory)
        await session.rollback()


# ---------------------------------------------------------------------------
# Step 8 fix-up regression tests (§7.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_rejects_name_null(session_factory: Any, tmp_path: Any) -> None:
    """PATCH name=null raises LLMModelInvalidConfigError (NOT NULL guard, §7.1)."""
    gguf = tmp_path / 'null-name.gguf'
    gguf.write_bytes(b'')
    factory = make_fake_factory()

    async with session_factory() as session:
        model = await create_llm_model(
            session,
            LLMModelCreate(
                name=f'null-name-{uuid.uuid4().hex[:8]}',
                provider=LLMProvider.llama_cpp,
                local_path=str(gguf),
            ),
        )
        model_id = model.id
        await session.commit()

    async with session_factory() as session:
        request = LLMModelUpdate.model_validate({'name': None})
        with pytest.raises(LLMModelInvalidConfigError, match="'name' cannot be null"):
            await update_llm_model(session, model_id, request, factory=factory)


@pytest.mark.asyncio
async def test_create_with_unknown_secret_id_returns_invalid_config(session_factory: Any) -> None:
    """openai model with non-existent secret_id raises LLMModelInvalidConfigError
    with a message about secret_id — NOT about 'dependent execution profiles' (§7.2)."""
    async with session_factory() as session:
        with pytest.raises(LLMModelInvalidConfigError, match='secret_id') as exc_info:
            await create_llm_model(
                session,
                LLMModelCreate(
                    name=f'oai-bad-secret-{uuid.uuid4().hex[:8]}',
                    provider=LLMProvider.openai,
                    endpoint_url='https://api.openai.com/v1',
                    model_ref='gpt-4o',
                    secret_id=uuid.uuid4(),
                ),
            )
        # Must NOT say "dependent execution profiles"
        assert 'dependent' not in str(exc_info.value).lower()
        await session.rollback()

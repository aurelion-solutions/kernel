# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Service-layer tests for LLMExecutionProfile CRUD.

All tests use the real async DB session fixture from src/conftest.py.
The preceding LLMModel row is inserted via ORM directly (not via service)
to keep profile tests independent of model-service coverage.
"""

from __future__ import annotations

from typing import Any
import uuid

import pytest
from src.platform.llm.exceptions import (
    LLMProfileInvalidConfigError,
    LLMProfileNameAlreadyExistsError,
    LLMProfileNotFoundError,
)
from src.platform.llm.models import LLMExecutionProfile, LLMModel, LLMProvider
from src.platform.llm.repository import get_profile_by_id
from src.platform.llm.schemas import LLMExecutionProfileCreate, LLMExecutionProfileUpdate
from src.platform.llm.service import (
    create_llm_profile,
    delete_llm_profile,
    get_llm_profile,
    list_llm_profiles,
    update_llm_profile,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _insert_model(session: Any) -> LLMModel:
    """Insert an LLMModel row directly via ORM (no service call)."""
    model = LLMModel(
        name=f'svc-test-model-{uuid.uuid4().hex[:8]}',
        provider=LLMProvider.ollama,
        endpoint_url='http://localhost:11434',
        model_ref='llama3',
    )
    session.add(model)
    await session.flush()
    await session.refresh(model)
    return model


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_minimal(session_factory: Any) -> None:
    """Create profile with valid name + existing model_id; row inserted."""
    async with session_factory() as session:
        model = await _insert_model(session)
        await session.commit()

    async with session_factory() as session:
        profile = await create_llm_profile(
            session,
            LLMExecutionProfileCreate(
                name=f'prof-{uuid.uuid4().hex[:8]}',
                model_id=model.id,
            ),
        )
        assert profile.id is not None
        assert profile.model_id == model.id
        assert profile.param_overrides == {}
        await session.rollback()


@pytest.mark.asyncio
async def test_create_with_param_overrides(session_factory: Any) -> None:
    """Non-empty param_overrides persisted as-is."""
    async with session_factory() as session:
        model = await _insert_model(session)
        await session.commit()

    overrides = {'temperature': 0.2, 'max_tokens': 512}
    async with session_factory() as session:
        profile = await create_llm_profile(
            session,
            LLMExecutionProfileCreate(
                name=f'prof-po-{uuid.uuid4().hex[:8]}',
                model_id=model.id,
                param_overrides=overrides,
            ),
        )
        assert profile.param_overrides == overrides
        await session.rollback()


@pytest.mark.asyncio
async def test_create_unknown_model_id_raises_invalid_config(session_factory: Any) -> None:
    """Random UUID model_id raises LLMProfileInvalidConfigError."""
    async with session_factory() as session:
        with pytest.raises(LLMProfileInvalidConfigError, match='model_id'):
            await create_llm_profile(
                session,
                LLMExecutionProfileCreate(
                    name=f'prof-nfk-{uuid.uuid4().hex[:8]}',
                    model_id=uuid.uuid4(),
                ),
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_create_duplicate_name_raises_already_exists(session_factory: Any) -> None:
    """Second create with same name raises LLMProfileNameAlreadyExistsError."""
    async with session_factory() as session:
        model = await _insert_model(session)
        await session.commit()

    name = f'dup-prof-{uuid.uuid4().hex[:8]}'

    async with session_factory() as session:
        await create_llm_profile(
            session,
            LLMExecutionProfileCreate(name=name, model_id=model.id),
        )
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(LLMProfileNameAlreadyExistsError):
            await create_llm_profile(
                session,
                LLMExecutionProfileCreate(name=name, model_id=model.id),
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_get_returns_row(session_factory: Any) -> None:
    """get_llm_profile returns the ORM instance for an existing id."""
    async with session_factory() as session:
        model = await _insert_model(session)
        profile = LLMExecutionProfile(name=f'get-prof-{uuid.uuid4().hex[:8]}', model_id=model.id)
        session.add(profile)
        await session.commit()

    async with session_factory() as session:
        fetched = await get_llm_profile(session, profile.id)
        assert fetched is not None
        assert fetched.id == profile.id


@pytest.mark.asyncio
async def test_get_unknown_returns_none(session_factory: Any) -> None:
    """get_llm_profile returns None for a random UUID."""
    async with session_factory() as session:
        result = await get_llm_profile(session, uuid.uuid4())
        assert result is None


@pytest.mark.asyncio
async def test_list_sorted_by_name_asc(session_factory: Any) -> None:
    """list_llm_profiles returns rows alphabetically by name."""
    async with session_factory() as session:
        model = await _insert_model(session)
        suffix = uuid.uuid4().hex[:6]
        names = [f'ccc-{suffix}', f'aaa-{suffix}', f'bbb-{suffix}']
        for n in names:
            session.add(LLMExecutionProfile(name=n, model_id=model.id))
        await session.commit()

    async with session_factory() as session:
        all_profiles = await list_llm_profiles(session)
        result_names = [p.name for p in all_profiles if p.name.endswith(suffix)]
        assert result_names == sorted(result_names)


@pytest.mark.asyncio
async def test_update_partial_changes_only_set_fields(session_factory: Any) -> None:
    """PATCH name only — param_overrides untouched."""
    async with session_factory() as session:
        model = await _insert_model(session)
        profile = LLMExecutionProfile(
            name=f'upd-partial-{uuid.uuid4().hex[:8]}',
            model_id=model.id,
            param_overrides={'temperature': 0.5},
        )
        session.add(profile)
        await session.commit()

    async with session_factory() as session:
        new_name = f'renamed-{uuid.uuid4().hex[:8]}'
        updated = await update_llm_profile(
            session,
            profile.id,
            LLMExecutionProfileUpdate(name=new_name),
        )
        assert updated.name == new_name
        assert updated.param_overrides == {'temperature': 0.5}
        await session.commit()


@pytest.mark.asyncio
async def test_update_replaces_param_overrides(session_factory: Any) -> None:
    """PATCH param_overrides replaces the dict (not merges)."""
    async with session_factory() as session:
        model = await _insert_model(session)
        profile = LLMExecutionProfile(
            name=f'upd-replace-{uuid.uuid4().hex[:8]}',
            model_id=model.id,
            param_overrides={'temperature': 0.5, 'top_p': 0.9},
        )
        session.add(profile)
        await session.commit()

    async with session_factory() as session:
        new_overrides = {'max_tokens': 256}
        updated = await update_llm_profile(
            session,
            profile.id,
            LLMExecutionProfileUpdate.model_validate({'param_overrides': new_overrides}),
        )
        assert updated.param_overrides == new_overrides
        await session.commit()


@pytest.mark.asyncio
async def test_update_unknown_id_raises_not_found(session_factory: Any) -> None:
    """update_llm_profile with random UUID raises LLMProfileNotFoundError."""
    async with session_factory() as session:
        with pytest.raises(LLMProfileNotFoundError):
            await update_llm_profile(
                session,
                uuid.uuid4(),
                LLMExecutionProfileUpdate(name='x'),
            )


@pytest.mark.asyncio
async def test_update_duplicate_name_raises_already_exists(session_factory: Any) -> None:
    """Renaming to an existing profile name raises LLMProfileNameAlreadyExistsError."""
    async with session_factory() as session:
        model = await _insert_model(session)
        name_a = f'name-a-{uuid.uuid4().hex[:8]}'
        name_b = f'name-b-{uuid.uuid4().hex[:8]}'
        for n in (name_a, name_b):
            session.add(LLMExecutionProfile(name=n, model_id=model.id))
        await session.commit()

    async with session_factory() as session:
        # fetch profile_b to get its id
        from src.platform.llm.repository import get_profile_by_name

        profile_b = await get_profile_by_name(session, name_b)
        assert profile_b is not None
        with pytest.raises(LLMProfileNameAlreadyExistsError):
            await update_llm_profile(
                session,
                profile_b.id,
                LLMExecutionProfileUpdate(name=name_a),
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_update_rejects_name_null(session_factory: Any) -> None:
    """PATCH name=null raises LLMProfileInvalidConfigError (NOT NULL guard)."""
    async with session_factory() as session:
        model = await _insert_model(session)
        profile = LLMExecutionProfile(
            name=f'null-name-{uuid.uuid4().hex[:8]}',
            model_id=model.id,
        )
        session.add(profile)
        await session.commit()

    async with session_factory() as session:
        request = LLMExecutionProfileUpdate.model_validate({'name': None})
        with pytest.raises(LLMProfileInvalidConfigError, match="'name' cannot be null"):
            await update_llm_profile(session, profile.id, request)


@pytest.mark.asyncio
async def test_update_rejects_param_overrides_null(session_factory: Any) -> None:
    """PATCH param_overrides=null raises LLMProfileInvalidConfigError."""
    async with session_factory() as session:
        model = await _insert_model(session)
        profile = LLMExecutionProfile(
            name=f'null-po-{uuid.uuid4().hex[:8]}',
            model_id=model.id,
        )
        session.add(profile)
        await session.commit()

    async with session_factory() as session:
        request = LLMExecutionProfileUpdate.model_validate({'param_overrides': None})
        with pytest.raises(LLMProfileInvalidConfigError, match="'param_overrides' cannot be null"):
            await update_llm_profile(session, profile.id, request)


@pytest.mark.asyncio
async def test_delete_removes_row(session_factory: Any) -> None:
    """delete_llm_profile removes row; subsequent get returns None."""
    async with session_factory() as session:
        model = await _insert_model(session)
        profile = LLMExecutionProfile(
            name=f'del-prof-{uuid.uuid4().hex[:8]}',
            model_id=model.id,
        )
        session.add(profile)
        await session.commit()

    profile_id = profile.id
    async with session_factory() as session:
        await delete_llm_profile(session, profile_id)
        await session.commit()

    async with session_factory() as session:
        assert await get_profile_by_id(session, profile_id) is None


@pytest.mark.asyncio
async def test_delete_unknown_id_raises_not_found(session_factory: Any) -> None:
    """delete_llm_profile with random UUID raises LLMProfileNotFoundError."""
    async with session_factory() as session:
        with pytest.raises(LLMProfileNotFoundError):
            await delete_llm_profile(session, uuid.uuid4())

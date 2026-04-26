# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP-layer tests for LLMExecutionProfile CRUD routes.

Uses the shared ``app`` / ``client`` / ``session_factory`` fixtures from
src/conftest.py.  Models are created via the model API or direct ORM inserts.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
import uuid

import pytest
from src.platform.llm.deps import get_llm_factory
from src.platform.llm.models import LLMModel, LLMProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_fake_factory() -> Any:
    factory = AsyncMock()
    factory.invalidate = AsyncMock()
    return factory


async def _create_model_via_api(client: Any, app: Any, tmp_path: Any) -> str:
    """Create a llama_cpp model via the API and return its id string."""
    gguf = tmp_path / f'model-{uuid.uuid4().hex[:8]}.gguf'
    gguf.write_bytes(b'')
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()
    resp = await client.post(
        '/api/v0/llm/models',
        json={
            'name': f'rt-model-{uuid.uuid4().hex[:8]}',
            'provider': 'llama_cpp',
            'local_path': str(gguf),
        },
    )
    assert resp.status_code == 201
    return str(resp.json()['id'])


async def _insert_model_orm(session_factory: Any) -> uuid.UUID:
    """Insert an LLMModel row directly via ORM and return its id."""
    async with session_factory() as session:
        model = LLMModel(
            name=f'rt-orm-model-{uuid.uuid4().hex[:8]}',
            provider=LLMProvider.ollama,
            endpoint_url='http://localhost:11434',
            model_ref='llama3',
        )
        session.add(model)
        await session.commit()
        return model.id


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_creates_201(app: Any, client: Any, tmp_path: Any) -> None:
    """POST /api/v0/llm/execution-profiles returns 201 with correct body shape."""
    model_id = await _create_model_via_api(client, app, tmp_path)
    try:
        resp = await client.post(
            '/api/v0/llm/execution-profiles',
            json={'name': f'prof-{uuid.uuid4().hex[:8]}', 'model_id': model_id},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert 'id' in data
        assert data['model_id'] == model_id
        assert data['param_overrides'] == {}
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_post_unknown_model_id_returns_422(app: Any, client: Any) -> None:
    """POST with a non-existent model_id returns 422."""
    resp = await client.post(
        '/api/v0/llm/execution-profiles',
        json={'name': f'prof-nfk-{uuid.uuid4().hex[:8]}', 'model_id': str(uuid.uuid4())},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_duplicate_name_returns_409(app: Any, client: Any, session_factory: Any) -> None:
    """Second POST with same name returns 409."""
    model_id = await _insert_model_orm(session_factory)
    name = f'dup-rt-{uuid.uuid4().hex[:8]}'

    r1 = await client.post(
        '/api/v0/llm/execution-profiles',
        json={'name': name, 'model_id': str(model_id)},
    )
    assert r1.status_code == 201

    r2 = await client.post(
        '/api/v0/llm/execution-profiles',
        json={'name': name, 'model_id': str(model_id)},
    )
    assert r2.status_code == 409


@pytest.mark.asyncio
async def test_get_list_returns_sorted_by_name(app: Any, client: Any, session_factory: Any) -> None:
    """GET /api/v0/llm/execution-profiles returns rows alphabetically by name."""
    model_id = await _insert_model_orm(session_factory)
    suffix = uuid.uuid4().hex[:6]
    names = [f'ccc-{suffix}', f'aaa-{suffix}', f'bbb-{suffix}']

    for name in names:
        r = await client.post(
            '/api/v0/llm/execution-profiles',
            json={'name': name, 'model_id': str(model_id)},
        )
        assert r.status_code == 201

    resp = await client.get('/api/v0/llm/execution-profiles')
    assert resp.status_code == 200
    result_names = [p['name'] for p in resp.json() if p['name'].endswith(suffix)]
    assert result_names == sorted(result_names)


@pytest.mark.asyncio
async def test_get_one_returns_200(app: Any, client: Any, session_factory: Any) -> None:
    """GET /api/v0/llm/execution-profiles/{id} returns 200 and correct body."""
    model_id = await _insert_model_orm(session_factory)
    name = f'get-one-{uuid.uuid4().hex[:8]}'

    r = await client.post(
        '/api/v0/llm/execution-profiles',
        json={'name': name, 'model_id': str(model_id)},
    )
    assert r.status_code == 201
    profile_id = r.json()['id']

    resp = await client.get(f'/api/v0/llm/execution-profiles/{profile_id}')
    assert resp.status_code == 200
    assert resp.json()['name'] == name


@pytest.mark.asyncio
async def test_get_one_missing_returns_404(app: Any, client: Any) -> None:
    """GET /api/v0/llm/execution-profiles/{random_uuid} returns 404."""
    resp = await client.get(f'/api/v0/llm/execution-profiles/{uuid.uuid4()}')
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_patch_updates_and_returns_200(app: Any, client: Any, session_factory: Any) -> None:
    """PATCH param_overrides returns 200 with updated value."""
    model_id = await _insert_model_orm(session_factory)

    r = await client.post(
        '/api/v0/llm/execution-profiles',
        json={'name': f'patch-prof-{uuid.uuid4().hex[:8]}', 'model_id': str(model_id)},
    )
    assert r.status_code == 201
    profile_id = r.json()['id']

    new_overrides = {'temperature': 0.1}
    resp = await client.patch(
        f'/api/v0/llm/execution-profiles/{profile_id}',
        json={'param_overrides': new_overrides},
    )
    assert resp.status_code == 200
    assert resp.json()['param_overrides'] == new_overrides


@pytest.mark.asyncio
async def test_patch_model_id_field_rejected(app: Any, client: Any, session_factory: Any) -> None:
    """PATCH body with model_id field returns 422 (extra='forbid')."""
    model_id = await _insert_model_orm(session_factory)

    r = await client.post(
        '/api/v0/llm/execution-profiles',
        json={'name': f'forbid-{uuid.uuid4().hex[:8]}', 'model_id': str(model_id)},
    )
    assert r.status_code == 201
    profile_id = r.json()['id']

    resp = await client.patch(
        f'/api/v0/llm/execution-profiles/{profile_id}',
        json={'model_id': str(uuid.uuid4())},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_name_null_returns_422(app: Any, client: Any, session_factory: Any) -> None:
    """PATCH {\"name\": null} returns 422 (NOT NULL guard in service)."""
    model_id = await _insert_model_orm(session_factory)

    r = await client.post(
        '/api/v0/llm/execution-profiles',
        json={'name': f'null-name-{uuid.uuid4().hex[:8]}', 'model_id': str(model_id)},
    )
    assert r.status_code == 201
    profile_id = r.json()['id']

    resp = await client.patch(
        f'/api/v0/llm/execution-profiles/{profile_id}',
        json={'name': None},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_delete_returns_204(app: Any, client: Any, session_factory: Any) -> None:
    """DELETE existing profile returns 204; subsequent GET returns 404."""
    model_id = await _insert_model_orm(session_factory)

    r = await client.post(
        '/api/v0/llm/execution-profiles',
        json={'name': f'del-rt-{uuid.uuid4().hex[:8]}', 'model_id': str(model_id)},
    )
    assert r.status_code == 201
    profile_id = r.json()['id']

    del_resp = await client.delete(f'/api/v0/llm/execution-profiles/{profile_id}')
    assert del_resp.status_code == 204

    get_resp = await client.get(f'/api/v0/llm/execution-profiles/{profile_id}')
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_missing_returns_404(app: Any, client: Any) -> None:
    """DELETE /api/v0/llm/execution-profiles/{random_uuid} returns 404."""
    resp = await client.delete(f'/api/v0/llm/execution-profiles/{uuid.uuid4()}')
    assert resp.status_code == 404

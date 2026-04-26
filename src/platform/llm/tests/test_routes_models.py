# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""HTTP-layer tests for LLM model CRUD routes.

Uses the shared ``app`` / ``client`` fixtures from src/conftest.py.
``get_llm_factory`` is overridden per test using dependency_overrides so real
providers are never loaded.  The factory's ``invalidate`` method is an
AsyncMock.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
import uuid

import pytest
from src.platform.llm.deps import get_llm_factory
from src.platform.llm.models import LLMExecutionProfile

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_fake_factory() -> Any:
    factory = AsyncMock()
    factory.invalidate = AsyncMock()
    return factory


def _llama_body(name: str, local_path: str) -> dict[str, Any]:
    return {
        'name': name,
        'provider': 'llama_cpp',
        'local_path': local_path,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_creates_201(app: Any, client: Any, tmp_path: Any) -> None:
    """POST /api/v0/llm/models with valid llama_cpp body returns 201."""
    gguf = tmp_path / 'model.gguf'
    gguf.write_bytes(b'')
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        resp = await client.post(
            '/api/v0/llm/models',
            json=_llama_body(f'route-m-{uuid.uuid4().hex[:8]}', str(gguf)),
        )
        assert resp.status_code == 201
        data = resp.json()
        assert 'id' in data
        assert data['provider'] == 'llama_cpp'
        assert data['is_active'] is True
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_post_invalid_combo_returns_422(app: Any, client: Any) -> None:
    """POST openai without secret_id returns 422."""
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        resp = await client.post(
            '/api/v0/llm/models',
            json={
                'name': f'bad-oai-{uuid.uuid4().hex[:8]}',
                'provider': 'openai',
                'endpoint_url': 'https://api.openai.com/v1',
                'model_ref': 'gpt-4o',
                # secret_id omitted
            },
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_post_duplicate_name_returns_409(app: Any, client: Any, tmp_path: Any) -> None:
    """Second POST with same name returns 409."""
    gguf = tmp_path / 'dup.gguf'
    gguf.write_bytes(b'')
    name = f'dup-route-{uuid.uuid4().hex[:8]}'
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        body = _llama_body(name, str(gguf))
        r1 = await client.post('/api/v0/llm/models', json=body)
        assert r1.status_code == 201

        r2 = await client.post('/api/v0/llm/models', json=body)
        assert r2.status_code == 409
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_get_list_returns_sorted_by_name(app: Any, client: Any, tmp_path: Any) -> None:
    """GET /api/v0/llm/models returns rows sorted alphabetically by name."""
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()
    names = [f'aaa-{uuid.uuid4().hex[:6]}', f'bbb-{uuid.uuid4().hex[:6]}', f'ccc-{uuid.uuid4().hex[:6]}']

    try:
        for name in names:
            gguf = tmp_path / f'{name}.gguf'
            gguf.write_bytes(b'')
            r = await client.post('/api/v0/llm/models', json=_llama_body(name, str(gguf)))
            assert r.status_code == 201

        resp = await client.get('/api/v0/llm/models')
        assert resp.status_code == 200
        result_names = [m['name'] for m in resp.json() if m['name'] in names]
        assert result_names == sorted(result_names)
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_get_one_returns_200(app: Any, client: Any, tmp_path: Any) -> None:
    """GET /api/v0/llm/models/{id} returns 200 and correct body shape."""
    gguf = tmp_path / 'one.gguf'
    gguf.write_bytes(b'')
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        name = f'get-one-{uuid.uuid4().hex[:8]}'
        r = await client.post('/api/v0/llm/models', json=_llama_body(name, str(gguf)))
        assert r.status_code == 201
        model_id = r.json()['id']

        resp = await client.get(f'/api/v0/llm/models/{model_id}')
        assert resp.status_code == 200
        data = resp.json()
        assert data['id'] == model_id
        assert data['name'] == name
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_get_one_missing_returns_404(app: Any, client: Any) -> None:
    """GET /api/v0/llm/models/{random_uuid} returns 404."""
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        resp = await client.get(f'/api/v0/llm/models/{uuid.uuid4()}')
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_patch_updates_and_returns_200(app: Any, client: Any, tmp_path: Any) -> None:
    """PATCH description returns 200 and updated value."""
    gguf = tmp_path / 'patch.gguf'
    gguf.write_bytes(b'')
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        r = await client.post(
            '/api/v0/llm/models',
            json=_llama_body(f'patch-me-{uuid.uuid4().hex[:8]}', str(gguf)),
        )
        assert r.status_code == 201
        model_id = r.json()['id']

        resp = await client.patch(
            f'/api/v0/llm/models/{model_id}',
            json={'description': 'updated desc'},
        )
        assert resp.status_code == 200
        assert resp.json()['description'] == 'updated desc'
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_patch_provider_field_rejected(app: Any, client: Any, tmp_path: Any) -> None:
    """PATCH body with provider field returns 422 (extra='forbid')."""
    gguf = tmp_path / 'prov.gguf'
    gguf.write_bytes(b'')
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        r = await client.post(
            '/api/v0/llm/models',
            json=_llama_body(f'prov-{uuid.uuid4().hex[:8]}', str(gguf)),
        )
        assert r.status_code == 201
        model_id = r.json()['id']

        resp = await client.patch(
            f'/api/v0/llm/models/{model_id}',
            json={'provider': 'ollama'},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_delete_returns_204(app: Any, client: Any, tmp_path: Any) -> None:
    """DELETE existing row returns 204; subsequent GET returns 404."""
    gguf = tmp_path / 'del.gguf'
    gguf.write_bytes(b'')
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        r = await client.post(
            '/api/v0/llm/models',
            json=_llama_body(f'del-{uuid.uuid4().hex[:8]}', str(gguf)),
        )
        assert r.status_code == 201
        model_id = r.json()['id']

        del_resp = await client.delete(f'/api/v0/llm/models/{model_id}')
        assert del_resp.status_code == 204

        get_resp = await client.get(f'/api/v0/llm/models/{model_id}')
        assert get_resp.status_code == 404
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_delete_missing_returns_404(app: Any, client: Any) -> None:
    """DELETE /api/v0/llm/models/{random_uuid} returns 404."""
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        resp = await client.delete(f'/api/v0/llm/models/{uuid.uuid4()}')
        assert resp.status_code == 404
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_delete_with_profile_returns_422(app: Any, client: Any, session_factory: Any, tmp_path: Any) -> None:
    """DELETE model with dependent profile returns 422."""
    gguf = tmp_path / 'prof.gguf'
    gguf.write_bytes(b'')
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        r = await client.post(
            '/api/v0/llm/models',
            json=_llama_body(f'profiled-{uuid.uuid4().hex[:8]}', str(gguf)),
        )
        assert r.status_code == 201
        model_id = r.json()['id']

        # Insert profile directly via ORM (Step 9 owns profile service)
        async with session_factory() as session:
            profile = LLMExecutionProfile(
                name=f'rt-profile-{uuid.uuid4().hex[:8]}',
                model_id=uuid.UUID(model_id),
            )
            session.add(profile)
            await session.commit()

        resp = await client.delete(f'/api/v0/llm/models/{model_id}')
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


# ---------------------------------------------------------------------------
# Step 8 fix-up regression tests (§7.3 API layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_name_null_returns_422(app: Any, client: Any, tmp_path: Any) -> None:
    """PATCH {\"name\": null} on existing model returns 422 (NOT NULL guard, §7.1)."""
    gguf = tmp_path / 'null-patch.gguf'
    gguf.write_bytes(b'')
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        r = await client.post(
            '/api/v0/llm/models',
            json=_llama_body(f'null-patch-{uuid.uuid4().hex[:8]}', str(gguf)),
        )
        assert r.status_code == 201
        model_id = r.json()['id']

        resp = await client.patch(
            f'/api/v0/llm/models/{model_id}',
            json={'name': None},
        )
        assert resp.status_code == 422
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)


@pytest.mark.asyncio
async def test_create_unknown_secret_id_returns_422(app: Any, client: Any) -> None:
    """POST openai model with non-existent secret_id returns 422 with 'secret_id' in detail (§7.2)."""
    app.dependency_overrides[get_llm_factory] = lambda: make_fake_factory()

    try:
        resp = await client.post(
            '/api/v0/llm/models',
            json={
                'name': f'oai-bad-sec-{uuid.uuid4().hex[:8]}',
                'provider': 'openai',
                'endpoint_url': 'https://api.openai.com/v1',
                'model_ref': 'gpt-4o',
                'secret_id': str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 422
        assert 'secret_id' in resp.json()['detail']
    finally:
        app.dependency_overrides.pop(get_llm_factory, None)

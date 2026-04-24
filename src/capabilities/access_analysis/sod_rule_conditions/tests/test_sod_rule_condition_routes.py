# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""API integration tests for /sod-rules/{rule_id}/conditions routes."""

from __future__ import annotations

import pytest

_SOD_RULES_BASE = '/api/v0/sod-rules'
_CAPS_BASE = '/api/v0/capabilities'


async def _create_rule(client, code: str = 'COND-RT-RULE') -> int:
    resp = await client.post(
        _SOD_RULES_BASE,
        json={'code': code, 'name': 'Test Rule', 'severity': 'high', 'scope_mode': 'global'},
    )
    assert resp.status_code == 201
    return resp.json()['id']


async def _create_capability(client, slug: str) -> int:
    resp = await client.post(_CAPS_BASE, json={'slug': slug, 'name': slug})
    if resp.status_code == 409:
        # Already exists — get id via list
        list_resp = await client.get(_CAPS_BASE)
        for cap in list_resp.json():
            if cap['slug'] == slug:
                return cap['id']
    return resp.json()['id']


def _cond_url(rule_id: int) -> str:
    return f'{_SOD_RULES_BASE}/{rule_id}/conditions'


@pytest.mark.asyncio
async def test_post_condition_valid_returns_201(client) -> None:
    cap_id = await _create_capability(client, 'cond_rt_cap1')
    rule_id = await _create_rule(client, 'COND-RT-001')

    resp = await client.post(_cond_url(rule_id), json={'capability_ids': [cap_id]})
    assert resp.status_code == 201
    body = resp.json()
    assert body['rule_id'] == rule_id
    assert body['capability_ids'] == [cap_id]
    assert body['min_count'] == 1


@pytest.mark.asyncio
async def test_post_condition_sorted_capability_ids(client) -> None:
    cap_id1 = await _create_capability(client, 'cond_sort_cap1')
    cap_id2 = await _create_capability(client, 'cond_sort_cap2')
    rule_id = await _create_rule(client, 'COND-RT-SORT')

    # Pass in reverse order
    resp = await client.post(
        _cond_url(rule_id),
        json={'capability_ids': sorted([cap_id1, cap_id2], reverse=True)},
    )
    assert resp.status_code == 201
    assert resp.json()['capability_ids'] == sorted([cap_id1, cap_id2])


@pytest.mark.asyncio
async def test_post_condition_empty_capability_ids_returns_422(client) -> None:
    rule_id = await _create_rule(client, 'COND-RT-EMPTY')
    resp = await client.post(_cond_url(rule_id), json={'capability_ids': []})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_condition_unknown_capability_ids_returns_422(client) -> None:
    cap_id = await _create_capability(client, 'cond_rt_known_cap')
    rule_id = await _create_rule(client, 'COND-RT-UNKNOWN-CAP')
    resp = await client.post(
        _cond_url(rule_id),
        json={'capability_ids': [cap_id, 999997]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_condition_unknown_rule_id_returns_404(client) -> None:
    cap_id = await _create_capability(client, 'cond_rt_no_rule_cap')
    resp = await client.post('/api/v0/sod-rules/99999/conditions', json={'capability_ids': [cap_id]})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_post_condition_min_count_zero_returns_422(client) -> None:
    cap_id = await _create_capability(client, 'cond_rt_zero_mc_cap')
    rule_id = await _create_rule(client, 'COND-RT-ZERO-MC')
    resp = await client.post(
        _cond_url(rule_id),
        json={'capability_ids': [cap_id], 'min_count': 0},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_conditions_list(client) -> None:
    cap_id1 = await _create_capability(client, 'cond_list_cap1')
    cap_id2 = await _create_capability(client, 'cond_list_cap2')
    rule_id = await _create_rule(client, 'COND-RT-LIST')

    await client.post(_cond_url(rule_id), json={'capability_ids': [cap_id1]})
    await client.post(_cond_url(rule_id), json={'capability_ids': [cap_id2]})

    resp = await client.get(_cond_url(rule_id))
    assert resp.status_code == 200
    conditions = resp.json()
    assert len(conditions) == 2


@pytest.mark.asyncio
async def test_get_condition_by_id_missing_returns_404(client) -> None:
    rule_id = await _create_rule(client, 'COND-RT-MISS')
    resp = await client.get(f'{_cond_url(rule_id)}/99999')
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_condition_returns_204(client) -> None:
    cap_id = await _create_capability(client, 'cond_del_cap')
    rule_id = await _create_rule(client, 'COND-RT-DEL')

    create_resp = await client.post(_cond_url(rule_id), json={'capability_ids': [cap_id]})
    cond_id = create_resp.json()['id']

    del_resp = await client.delete(f'{_cond_url(rule_id)}/{cond_id}')
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_condition_second_delete_returns_404(client) -> None:
    cap_id = await _create_capability(client, 'cond_double_del_cap')
    rule_id = await _create_rule(client, 'COND-RT-DDEL')

    create_resp = await client.post(_cond_url(rule_id), json={'capability_ids': [cap_id]})
    cond_id = create_resp.json()['id']

    await client.delete(f'{_cond_url(rule_id)}/{cond_id}')
    second = await client.delete(f'{_cond_url(rule_id)}/{cond_id}')
    assert second.status_code == 404

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Env-gated parity smoke test: normalized.access_facts invariants.

Skipped by default (no env gate set). Run locally or in a dedicated nightly job by
setting ``LAKE_FACTS_PARITY=1`` (or any non-empty value).

Parity contract (smoke-only, per user approval):
  - fact_count == N (no rows silently dropped)
  - unique natural_key_hash count == N (no duplicates)
  - is_active=True ↔ valid_until IS NULL
  - every fact has non-null reconciliation_delta_item_id
  - effect distribution matches fixture expected.effect_distribution

On mismatch: pytest.fail with first-10-diff output.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from src.integration_tests.conftest import (
    build_artifact_items,
    load_pipeline_dataset,
    seed_pipeline_inventory,
)

# ---------------------------------------------------------------------------
# Env gate
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    os.getenv('LAKE_FACTS_PARITY') is None,
    reason='LAKE_FACTS_PARITY env var not set — parity test skipped',
)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lake_facts_match_golden_fixture(
    client_iceberg: Any,
    session_factory: Any,
    app_iceberg: Any,
) -> None:
    """Smoke parity: run full auto_apply pipeline, verify normalized.access_facts invariants."""
    import uuid

    dataset = load_pipeline_dataset()
    expected = dataset['expected']

    # Seed PG master data
    async with session_factory() as session:
        refs = await seed_pipeline_inventory(session, dataset)
        await session.commit()

    app_id = str(refs['app_id'])
    ingest_batch_id = str(uuid.uuid4())

    # Step 1: Bulk ingest artifacts
    items = build_artifact_items(dataset, refs)
    bulk_resp = await client_iceberg.post(
        '/api/v0/access-artifacts/bulk',
        json={'ingest_batch_id': ingest_batch_id, 'items': items},
    )
    assert bulk_resp.status_code == 200, f'bulk upsert failed: {bulk_resp.text}'

    # Step 2: auto_apply reconciliation (reconcile + apply in one request)
    recon_resp = await client_iceberg.post(
        '/api/v0/reconciliation/runs',
        json={'application_id': app_id, 'mode': 'auto_apply'},
    )
    assert recon_resp.status_code == 200, f'reconciliation failed: {recon_resp.text}'
    recon_data = recon_resp.json()
    assert recon_data['status'] == 'applied', f'Expected status=applied after auto_apply, got {recon_data["status"]}'

    # Step 3: Query normalized.access_facts via DuckDB and project to tuples
    lake_settings = app_iceberg.state.test_lake_settings
    warehouse_uri = lake_settings.warehouse_uri
    factory = app_iceberg.state.lake_session_factory
    lake_session = factory.acquire()
    try:
        rows = lake_session.execute(
            f"""
            SELECT
                natural_key_hash,
                effect,
                is_active,
                valid_until,
                reconciliation_delta_item_id
            FROM iceberg_scan('{warehouse_uri}/normalized/access_facts')
            ORDER BY natural_key_hash
            """
        ).fetchall()
    finally:
        lake_session.__exit__(None, None, None)

    # Step 4: Smoke assertions
    _assert_parity_invariants(rows, expected)


# ---------------------------------------------------------------------------
# Invariant checker
# ---------------------------------------------------------------------------


def _assert_parity_invariants(
    rows: list[Any],
    expected: dict[str, Any],
) -> None:
    """Assert all parity smoke invariants. Call pytest.fail with diff on mismatch."""
    failures: list[str] = []

    # 1. fact_count
    if len(rows) != expected['fact_count']:
        failures.append(f'fact_count: expected={expected["fact_count"]}, actual={len(rows)}')

    # 2. unique natural_key_hash count (no duplicates)
    hashes = [r[0] for r in rows]
    unique_count = len(set(hashes))
    if unique_count != expected['fact_count']:
        duplicate_hashes = [h for h in hashes if hashes.count(h) > 1][:10]
        failures.append(
            f'unique_hash_count: expected={expected["fact_count"]}, '
            f'actual={unique_count}; sample duplicates={duplicate_hashes}'
        )

    # 3. is_active=True ↔ valid_until IS NULL
    inconsistent_active = [
        {'natural_key_hash': r[0], 'is_active': r[2], 'valid_until': r[3]}
        for r in rows
        if r[2] is True and r[3] is not None
    ][:10]
    if inconsistent_active:
        failures.append(
            f'is_active=True with non-null valid_until ({len(inconsistent_active)} rows): {inconsistent_active}'
        )

    # 4. every fact has non-null reconciliation_delta_item_id
    null_delta = [r[0] for r in rows if r[4] is None][:10]
    if null_delta:
        failures.append(f'{len(null_delta)} facts have null reconciliation_delta_item_id; sample hashes={null_delta}')

    # 5. effect distribution
    effect_counts: dict[str, int] = {}
    for r in rows:
        effect = r[1]
        effect_counts[effect] = effect_counts.get(effect, 0) + 1

    exp_dist = expected['effect_distribution']
    dist_failures = []
    for effect, count in exp_dist.items():
        actual = effect_counts.get(effect, 0)
        if actual != count:
            dist_failures.append(f'{effect}: expected={count}, actual={actual}')
    # Check for unexpected effects
    for effect in effect_counts:
        if effect not in exp_dist:
            dist_failures.append(f'unexpected effect={effect}: count={effect_counts[effect]}')
    if dist_failures:
        failures.append(f'effect_distribution mismatch: {dist_failures}')

    if failures:
        diff_lines = '\n'.join(f'  - {f}' for f in failures)
        pytest.fail(f'Lake facts parity invariants violated ({len(failures)} failure(s)):\n{diff_lines}')

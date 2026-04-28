# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Phase 15 Step 19 — e2e integration test: lake-only pipeline.

Drives the full pipeline through public API:
  Stage 1  — Seed PG master data (Application, Subjects, Accounts, Resources)
  Stage 2  — POST /access-artifacts/bulk (50 artifacts)
  Stage 3  — POST /reconciliation/runs (mode=review) → pending_apply
  Stage 4  — POST /reconciliation/runs/{id}/apply (mode=manual_apply) → applied
  Stage 5  — POST /scan-runs + POST /scan-runs/{id}/run → findings check
  Stage 6  — Idempotency: re-apply same run → 409
  Stage 7  — Invariant: no inventory.access_fact.* events during reconciliation step

This test runs unconditionally in CI (no env gate). Target wall-clock ≤ 30 s.
"""

from __future__ import annotations

from typing import Any
import uuid

import pytest
from src.integration_tests.conftest import (
    build_artifact_items,
    get_event_types,
    load_phase15_dataset,
    seed_inventory_for_p15,
)


@pytest.mark.asyncio
async def test_phase15_e2e_lake_only_pipeline(client_p15: Any, session_factory: Any, app_p15: Any) -> None:
    """Full Phase 15 lake-only pipeline: bulk ingest → reconciliation → apply → scan."""

    dataset = load_phase15_dataset()
    expected = dataset['expected']

    # -----------------------------------------------------------------------
    # Stage 1 — Seed PG master data
    # -----------------------------------------------------------------------
    async with session_factory() as session:
        refs = await seed_inventory_for_p15(session, dataset)
        await session.commit()

    app_id = str(refs['app_id'])
    ingest_batch_id = str(uuid.uuid4())

    # -----------------------------------------------------------------------
    # Stage 2 — POST /access-artifacts/bulk
    # -----------------------------------------------------------------------
    items = build_artifact_items(dataset, refs)
    bulk_resp = await client_p15.post(
        '/api/v0/access-artifacts/bulk',
        json={
            'ingest_batch_id': ingest_batch_id,
            'items': items,
        },
    )
    assert bulk_resp.status_code == 200, f'bulk upsert failed: {bulk_resp.text}'
    bulk_data = bulk_resp.json()
    assert bulk_data['row_count'] == len(dataset['artifacts']), (
        f'Expected {len(dataset["artifacts"])} rows, got {bulk_data["row_count"]}'
    )
    assert bulk_data['snapshot_id'] is not None, 'snapshot_id must be set after Iceberg write'
    assert bulk_data['backend'] == 'iceberg'

    # -----------------------------------------------------------------------
    # Stage 3 — POST /reconciliation/runs (mode=review)
    # -----------------------------------------------------------------------
    recon_resp = await client_p15.post(
        '/api/v0/reconciliation/runs',
        json={'application_id': app_id, 'mode': 'review'},
    )
    assert recon_resp.status_code == 200, f'reconciliation failed: {recon_resp.text}'
    recon_data = recon_resp.json()
    run_id: str = recon_data['id']

    # After review, run should be pending_apply with created_count == artifact count
    assert recon_data['status'] == 'pending_apply', f'Expected pending_apply, got {recon_data["status"]}'
    assert recon_data['created_count'] == expected['fact_count'], (
        f'Expected {expected["fact_count"]} created, got {recon_data["created_count"]}'
    )

    # Stage 7 invariant check (part 1): no inventory.access_fact.* events during reconcile
    event_types_after_recon = get_event_types(app_p15.state.event_buffer)
    fact_events_during_recon = [et for et in event_types_after_recon if et.startswith('inventory.access_fact.')]
    assert fact_events_during_recon == [], (
        f'inventory.access_fact.* events emitted during reconciliation: {fact_events_during_recon}'
    )

    # -----------------------------------------------------------------------
    # Stage 4 — POST /reconciliation/runs/{id}/apply (mode=manual_apply)
    # -----------------------------------------------------------------------
    apply_resp = await client_p15.post(
        f'/api/v0/reconciliation/runs/{run_id}/apply',
        json={'mode': 'manual_apply'},
    )
    assert apply_resp.status_code == 200, f'apply failed: {apply_resp.text}'
    apply_data = apply_resp.json()

    assert apply_data['applied_count'] == expected['fact_count'], (
        f'Expected {expected["fact_count"]} applied, got {apply_data["applied_count"]}'
    )
    assert apply_data['failed_count'] == 0, f'Expected 0 failed, got {apply_data["failed_count"]}'
    assert apply_data['status'] == 'completed', f'Expected completed, got {apply_data["status"]}'

    # Verify inventory.access_fact.created events emitted after apply
    event_types_after_apply = get_event_types(app_p15.state.event_buffer)
    fact_created_events = [et for et in event_types_after_apply if et == 'inventory.access_fact.created']
    assert len(fact_created_events) == expected['fact_count'], (
        f'Expected {expected["fact_count"]} inventory.access_fact.created events, got {len(fact_created_events)}'
    )

    # Smoke-check Iceberg: query normalized.access_facts via DuckDB
    _verify_facts_in_iceberg(app_p15, expected)

    # -----------------------------------------------------------------------
    # Stage 5 — Scan run: create + execute
    # -----------------------------------------------------------------------
    scan_create_resp = await client_p15.post(
        '/api/v0/scan-runs',
        json={'triggered_by': 'api'},
    )
    assert scan_create_resp.status_code == 201, f'scan create failed: {scan_create_resp.text}'
    scan_run_id: int = scan_create_resp.json()['id']

    scan_run_resp = await client_p15.post(f'/api/v0/scan-runs/{scan_run_id}/run')
    assert scan_run_resp.status_code == 200, f'scan run failed: {scan_run_resp.text}'
    scan_data = scan_run_resp.json()
    assert scan_data['status'] == 'completed', f'Expected completed, got {scan_data["status"]}'
    # findings_created_count >= 0 (may be 0 if no SoD rules configured — that is fine)
    assert isinstance(scan_data['findings_created_count'], int)

    # -----------------------------------------------------------------------
    # Stage 6 — Idempotency: re-apply same run → 409
    # -----------------------------------------------------------------------
    reapply_resp = await client_p15.post(
        f'/api/v0/reconciliation/runs/{run_id}/apply',
        json={'mode': 'manual_apply'},
    )
    assert reapply_resp.status_code == 409, (
        f'Re-apply should return 409, got {reapply_resp.status_code}: {reapply_resp.text}'
    )

    # Counts unchanged after 409
    _verify_facts_in_iceberg(app_p15, expected)

    # Stage 7 invariant (final check): count fact events = exactly N (no duplicates)
    event_types_final = get_event_types(app_p15.state.event_buffer)
    fact_events_final = [et for et in event_types_final if et.startswith('inventory.access_fact.')]
    assert len(fact_events_final) == expected['fact_count'], (
        f'Expected exactly {expected["fact_count"]} inventory.access_fact.* events total, '
        f'got {len(fact_events_final)}: {fact_events_final}'
    )


# ---------------------------------------------------------------------------
# Iceberg smoke helpers
# ---------------------------------------------------------------------------


def _verify_facts_in_iceberg(app: Any, expected: dict[str, Any]) -> None:
    """Smoke-check normalized.access_facts via DuckDB iceberg_scan.

    Assertions:
    - fact_count == N
    - unique natural_key_hash count == N (no duplicates)
    - all facts have non-null reconciliation_delta_item_id
    - is_active=True ↔ valid_until IS NULL (all new facts should satisfy this)
    - effect distribution matches expected
    """
    lake_settings = app.state.test_lake_settings
    warehouse_uri = lake_settings.warehouse_uri

    factory = app.state.lake_session_factory
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
            """
        ).fetchall()
    finally:
        lake_session.__exit__(None, None, None)

    fact_count = len(rows)
    assert fact_count == expected['fact_count'], f'Iceberg fact_count={fact_count}, expected={expected["fact_count"]}'

    # No duplicate natural_key_hash
    hashes = [r[0] for r in rows]
    unique_hash_count = len(set(hashes))
    assert unique_hash_count == expected['fact_count'], (
        f'Duplicate natural_key_hash detected: {fact_count} rows but {unique_hash_count} unique hashes'
    )

    # All facts have non-null reconciliation_delta_item_id
    null_delta_ids = [r for r in rows if r[4] is None]
    assert null_delta_ids == [], f'{len(null_delta_ids)} facts have null reconciliation_delta_item_id'

    # is_active=True ↔ valid_until IS NULL for all new facts
    is_active_col = [r[2] for r in rows]
    inconsistent = [(r[0], r[2], r[3]) for r in rows if r[2] is True and r[3] is not None]
    assert inconsistent == [], f'is_active=True facts with non-null valid_until: {inconsistent}'
    # Also verify all facts are active (first ingest, no revocations)
    inactive = [r for r, ia in zip(rows, is_active_col) if not ia]
    assert inactive == [], f'{len(inactive)} facts unexpectedly inactive'

    # Effect distribution
    effect_counts: dict[str, int] = {}
    for r in rows:
        effect = r[1]
        effect_counts[effect] = effect_counts.get(effect, 0) + 1

    exp_dist = expected['effect_distribution']
    for effect, count in exp_dist.items():
        actual = effect_counts.get(effect, 0)
        assert actual == count, f'Effect distribution mismatch: effect={effect}, expected={count}, actual={actual}'

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests for migration_writer.py helper functions."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock
import uuid

from pyiceberg.io.pyarrow import schema_to_pyarrow
from src.engines.lake_migration.migration_writer import (
    append_artifact_batch,
    append_fact_batch,
    build_artifact_arrow_table,
    build_fact_arrow_table,
)
from src.platform.lake.schemas import (
    NORMALIZED_ACCESS_FACTS_TABLE,
    RAW_ACCESS_ARTIFACTS_SCHEMA,
    RAW_ACCESS_ARTIFACTS_TABLE,
    RAW_NORMALIZED_ACCESS_FACTS_SCHEMA_FIELDS,
)


def _artifact_row(app_id: uuid.UUID | None = None) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.application_id = app_id or uuid.uuid4()
    row.artifact_type = 'role_assignment'
    row.external_id = 'ext-123'
    row.payload = {'key': 'value'}
    row.raw_name = 'some_role'
    row.effect = 'allow'
    row.valid_from = datetime(2026, 1, 1, tzinfo=UTC)
    row.valid_until = None
    row.is_active = True
    row.tombstoned_at = None
    row.observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    row.ingested_at = datetime(2026, 1, 1, tzinfo=UTC)
    row.ingest_batch_id = None
    return row


def _fact_row(
    app_id: uuid.UUID | None = None,
    subj_id: uuid.UUID | None = None,
    res_id: uuid.UUID | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.subject_id = subj_id or uuid.uuid4()
    row.account_id = None
    row.resource_id = res_id or uuid.uuid4()
    row.action_id = 1
    row.effect = MagicMock()
    row.effect.value = 'allow'
    row.valid_from = datetime(2026, 1, 1, tzinfo=UTC)
    row.valid_until = None
    row.is_active = True
    row.observed_at = datetime(2026, 1, 1, tzinfo=UTC)
    row.created_at = datetime(2026, 1, 1, tzinfo=UTC)
    row.revoked_at = None
    row.latest_batch_id = None
    return row


class TestBuildArtifactArrowTable:
    def test_column_order_matches_schema(self, catalog):
        """Column order must match RAW_ACCESS_ARTIFACTS_SCHEMA field order."""
        iceberg_tbl = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
        pa_schema = schema_to_pyarrow(iceberg_tbl.schema())

        rows = [_artifact_row() for _ in range(3)]
        tbl = build_artifact_arrow_table(rows, pa_schema=pa_schema)

        expected_cols = [f.name for f in RAW_ACCESS_ARTIFACTS_SCHEMA.fields]
        assert list(tbl.schema.names) == expected_cols

    def test_payload_is_json_string(self, catalog):
        iceberg_tbl = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
        pa_schema = schema_to_pyarrow(iceberg_tbl.schema())

        row = _artifact_row()
        row.payload = {'hello': 'world'}
        tbl = build_artifact_arrow_table([row], pa_schema=pa_schema)

        payload_vals = tbl.column('payload').to_pylist()
        assert payload_vals[0] == '{"hello": "world"}'

    def test_uuid_identity_preserved(self, catalog):
        """Original UUID must appear in the Iceberg row (no new uuid4 minted)."""
        iceberg_tbl = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
        pa_schema = schema_to_pyarrow(iceberg_tbl.schema())

        row = _artifact_row()
        original_id = row.id
        tbl = build_artifact_arrow_table([row], pa_schema=pa_schema)

        ids = tbl.column('id').to_pylist()
        # May be UUID object or string depending on pa_type
        result_id_str = str(ids[0]).lower().replace('-', '') if ids[0] is not None else ''
        original_str = str(original_id).lower().replace('-', '')
        assert original_str in result_id_str or result_id_str in original_str or str(original_id) == str(ids[0])

    def test_row_count_matches(self, catalog):
        iceberg_tbl = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
        pa_schema = schema_to_pyarrow(iceberg_tbl.schema())
        rows = [_artifact_row() for _ in range(10)]
        tbl = build_artifact_arrow_table(rows, pa_schema=pa_schema)
        assert len(tbl) == 10


class TestBuildFactArrowTable:
    def _pa_schema(self, catalog):
        from pyiceberg.io.pyarrow import schema_to_pyarrow  # noqa: PLC0415

        return schema_to_pyarrow(catalog.load_table(NORMALIZED_ACCESS_FACTS_TABLE).schema())

    def test_column_order_matches_schema(self, catalog):
        pa_schema = self._pa_schema(catalog)
        expected_cols = [f.name for f in RAW_NORMALIZED_ACCESS_FACTS_SCHEMA_FIELDS]

        fact = _fact_row()
        denorm_map = {fact.id: (uuid.uuid4(), 'employee')}
        delta_map = {fact.id: uuid.uuid4()}
        nk_map = {fact.id: 'a' * 64}

        tbl = build_fact_arrow_table(
            [fact],
            pa_schema=pa_schema,
            denorm_map=denorm_map,
            delta_item_id_map=delta_map,
            natural_key_hash_map=nk_map,
        )
        assert list(tbl.schema.names) == expected_cols

    def test_delta_item_id_populated(self, catalog):
        pa_schema = self._pa_schema(catalog)
        fact = _fact_row()
        delta_id = uuid.uuid4()
        denorm_map = {fact.id: (uuid.uuid4(), 'employee')}
        delta_map = {fact.id: delta_id}
        nk_map = {fact.id: 'b' * 64}

        tbl = build_fact_arrow_table(
            [fact],
            pa_schema=pa_schema,
            denorm_map=denorm_map,
            delta_item_id_map=delta_map,
            natural_key_hash_map=nk_map,
        )
        di_vals = tbl.column('reconciliation_delta_item_id').to_pylist()
        assert di_vals[0] is not None

    def test_natural_key_hash_populated(self, catalog):
        pa_schema = self._pa_schema(catalog)
        fact = _fact_row()
        nk_hash = 'c' * 64
        denorm_map = {fact.id: (uuid.uuid4(), 'employee')}
        delta_map = {fact.id: uuid.uuid4()}
        nk_map = {fact.id: nk_hash}

        tbl = build_fact_arrow_table(
            [fact],
            pa_schema=pa_schema,
            denorm_map=denorm_map,
            delta_item_id_map=delta_map,
            natural_key_hash_map=nk_map,
        )
        nk_vals = tbl.column('natural_key_hash').to_pylist()
        assert nk_vals[0] == nk_hash


class TestAppendBatches:
    def test_append_artifact_batch_returns_snapshot_id(self, catalog):
        iceberg_tbl = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
        pa_schema = schema_to_pyarrow(iceberg_tbl.schema())

        rows = [_artifact_row() for _ in range(5)]
        tbl = build_artifact_arrow_table(rows, pa_schema=pa_schema)
        snapshot_id = append_artifact_batch(catalog, tbl)
        assert isinstance(snapshot_id, int)
        assert snapshot_id != -1

    def test_append_fact_batch_returns_snapshot_id(self, catalog):
        pa_schema = schema_to_pyarrow(catalog.load_table(NORMALIZED_ACCESS_FACTS_TABLE).schema())

        fact = _fact_row()
        denorm_map = {fact.id: (uuid.uuid4(), 'employee')}
        delta_map = {fact.id: uuid.uuid4()}
        nk_map = {fact.id: 'd' * 64}

        tbl = build_fact_arrow_table(
            [fact],
            pa_schema=pa_schema,
            denorm_map=denorm_map,
            delta_item_id_map=delta_map,
            natural_key_hash_map=nk_map,
        )
        snapshot_id = append_fact_batch(catalog, tbl)
        assert isinstance(snapshot_id, int)
        assert snapshot_id != -1

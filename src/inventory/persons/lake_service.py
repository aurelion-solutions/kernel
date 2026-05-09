# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""PersonLakeService — writes raw.persons to the Iceberg lake.

Append-only with retire-on-re-upload: when the same external_id arrives in a
new batch, the old active row is overwritten (is_active=False, tombstoned_at)
and the new row is the authoritative version.

No reads, no PG access.  Reconciliation (Phase 2) reads from the lake and
applies to PG.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from pyiceberg.catalog import Catalog
from src.inventory.persons.schemas import PersonBulkItem
from src.platform.lake.raw_master_writer import (
    MasterDataBatchResult,
    latest_snapshot_id,
    new_row_id,
    retire_and_commit,
    scan_active_by_keys,
    ts_micros,
)
from src.platform.lake.schemas import RAW_PERSONS_TABLE


class PersonLakeNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__('PersonLakeService requires lake_catalog')


class PersonLakeWriteError(Exception):
    def __init__(self, message: str, *, cause: Exception) -> None:
        self.cause = cause
        super().__init__(message)


class PersonLakeService:
    """Writes PersonBulkItem batches to raw.persons (Iceberg lake)."""

    def __init__(self, lake_catalog: Catalog | None = None) -> None:
        self._catalog = lake_catalog

    async def upsert_batch(
        self,
        items: list[PersonBulkItem],
        *,
        ingest_batch_id: uuid.UUID,
        source_name: str | None = None,
    ) -> MasterDataBatchResult:
        """Append persons to lake; retire previous rows with same external_id."""
        if self._catalog is None:
            raise PersonLakeNotConfiguredError()

        try:
            table = self._catalog.load_table(RAW_PERSONS_TABLE)
            pa_schema = table.schema().as_arrow()
            now = datetime.now(UTC)
            arrow_new = _build_arrow(
                items, pa_schema=pa_schema, now=now, ingest_batch_id=ingest_batch_id, source_name=source_name
            )
            table.append(arrow_new)

            input_keys = {item.external_id for item in items}
            new_ids = set(arrow_new.column('id').to_pylist())
            scan = scan_active_by_keys(table, key_column='external_id', keys=list(input_keys))
            snap = retire_and_commit(
                table,
                scan_arrow=scan,
                key_column='external_id',
                input_keys=input_keys,
                new_ids=new_ids,
                observed_at=now,
            )
            if snap is None:
                snap = latest_snapshot_id(table)
        except (PersonLakeNotConfiguredError, PersonLakeWriteError):
            raise
        except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
            raise PersonLakeWriteError(f'raw.persons write failed: {exc}', cause=exc) from exc

        return MasterDataBatchResult(row_count=len(items), snapshot_id=snap, backend='iceberg')


# ---------------------------------------------------------------------------
# Arrow builder
# ---------------------------------------------------------------------------


def _build_arrow(
    items: list[PersonBulkItem],
    *,
    pa_schema: object,
    now: datetime,
    ingest_batch_id: uuid.UUID,
    source_name: str | None,
) -> object:
    import pyarrow as pa  # noqa: PLC0415

    now_us = ts_micros(now)
    batch_id_str = str(ingest_batch_id)
    tz_us = pa.timestamp('us', tz='UTC')

    ids, external_ids, full_names = [], [], []
    is_actives, tombstoned_ats, source_names = [], [], []
    ingest_batch_ids, observed_ats, ingested_ats = [], [], []

    for item in items:
        ids.append(new_row_id())
        external_ids.append(item.external_id)
        full_names.append(item.full_name)
        is_actives.append(True)
        tombstoned_ats.append(None)
        source_names.append(source_name)
        ingest_batch_ids.append(batch_id_str)
        observed_ats.append(now_us)
        ingested_ats.append(now_us)

    return pa.table(
        {
            'id': pa.array(ids, type=pa.string()),
            'external_id': pa.array(external_ids, type=pa.string()),
            'full_name': pa.array(full_names, type=pa.string()),
            'is_active': pa.array(is_actives, type=pa.bool_()),
            'tombstoned_at': pa.array(tombstoned_ats, type=tz_us),
            'source_name': pa.array(source_names, type=pa.string()),
            'ingest_batch_id': pa.array(ingest_batch_ids, type=pa.string()),
            'observed_at': pa.array(observed_ats, type=tz_us),
            'ingested_at': pa.array(ingested_ats, type=tz_us),
        },
        schema=pa_schema,
    )

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""OrgUnitLakeService — writes raw.org_units to the Iceberg lake."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from pyiceberg.catalog import Catalog
from src.inventory.org_units.schemas import OrgUnitBulkItem
from src.platform.lake.raw_master_writer import (
    MasterDataBatchResult,
    latest_snapshot_id,
    new_row_id,
    retire_and_commit,
    scan_active_by_keys,
    ts_micros,
)
from src.platform.lake.schemas import RAW_ORG_UNITS_TABLE


class OrgUnitLakeNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__('OrgUnitLakeService requires lake_catalog')


class OrgUnitLakeWriteError(Exception):
    def __init__(self, message: str, *, cause: Exception) -> None:
        self.cause = cause
        super().__init__(message)


class OrgUnitLakeService:
    """Writes OrgUnitBulkItem batches to raw.org_units (Iceberg lake)."""

    def __init__(self, lake_catalog: Catalog | None = None) -> None:
        self._catalog = lake_catalog

    async def upsert_batch(
        self,
        items: list[OrgUnitBulkItem],
        *,
        ingest_batch_id: uuid.UUID,
        source_name: str | None = None,
    ) -> MasterDataBatchResult:
        """Append org units to lake; retire previous rows with same external_id."""
        if self._catalog is None:
            raise OrgUnitLakeNotConfiguredError()

        try:
            table = self._catalog.load_table(RAW_ORG_UNITS_TABLE)
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
        except (OrgUnitLakeNotConfiguredError, OrgUnitLakeWriteError):
            raise
        except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
            raise OrgUnitLakeWriteError(f'raw.org_units write failed: {exc}', cause=exc) from exc

        return MasterDataBatchResult(row_count=len(items), snapshot_id=snap, backend='iceberg')


# ---------------------------------------------------------------------------
# Arrow builder
# ---------------------------------------------------------------------------


def _build_arrow(
    items: list[OrgUnitBulkItem],
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

    ids, external_ids, names, parent_external_ids = [], [], [], []
    is_actives, tombstoned_ats, source_names = [], [], []
    ingest_batch_ids, observed_ats, ingested_ats = [], [], []

    for item in items:
        ids.append(new_row_id())
        external_ids.append(item.external_id)
        names.append(item.name)
        parent_external_ids.append(item.parent_external_id)
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
            'name': pa.array(names, type=pa.string()),
            'parent_external_id': pa.array(parent_external_ids, type=pa.string()),
            'is_active': pa.array(is_actives, type=pa.bool_()),
            'tombstoned_at': pa.array(tombstoned_ats, type=tz_us),
            'source_name': pa.array(source_names, type=pa.string()),
            'ingest_batch_id': pa.array(ingest_batch_ids, type=pa.string()),
            'observed_at': pa.array(observed_ats, type=tz_us),
            'ingested_at': pa.array(ingested_ats, type=tz_us),
        },
        schema=pa_schema,
    )

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""EmployeeLakeService — writes raw.employees to the Iceberg lake.

Key is person_external_id (one employee record per person).
attributes dict is JSON-serialized into the lake column for later reconciliation.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import uuid

from pyiceberg.catalog import Catalog
from src.inventory.employees.schemas import EmployeeBulkItem
from src.platform.lake.raw_master_writer import (
    MasterDataBatchResult,
    latest_snapshot_id,
    new_row_id,
    retire_and_commit,
    scan_active_by_keys,
    ts_micros,
)
from src.platform.lake.schemas import RAW_EMPLOYEES_TABLE


class EmployeeLakeNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__('EmployeeLakeService requires lake_catalog')


class EmployeeLakeWriteError(Exception):
    def __init__(self, message: str, *, cause: Exception) -> None:
        self.cause = cause
        super().__init__(message)


class EmployeeLakeService:
    """Writes EmployeeBulkItem batches to raw.employees (Iceberg lake)."""

    def __init__(self, lake_catalog: Catalog | None = None) -> None:
        self._catalog = lake_catalog

    async def upsert_batch(
        self,
        items: list[EmployeeBulkItem],
        *,
        ingest_batch_id: uuid.UUID,
        source_name: str | None = None,
    ) -> MasterDataBatchResult:
        """Append employees to lake; retire previous rows with same person_external_id."""
        if self._catalog is None:
            raise EmployeeLakeNotConfiguredError()

        try:
            table = self._catalog.load_table(RAW_EMPLOYEES_TABLE)
            pa_schema = table.schema().as_arrow()
            now = datetime.now(UTC)
            arrow_new = _build_arrow(
                items, pa_schema=pa_schema, now=now, ingest_batch_id=ingest_batch_id, source_name=source_name
            )
            table.append(arrow_new)

            input_keys = {item.person_external_id for item in items}
            new_ids = set(arrow_new.column('id').to_pylist())
            scan = scan_active_by_keys(table, key_column='person_external_id', keys=list(input_keys))
            snap = retire_and_commit(
                table,
                scan_arrow=scan,
                key_column='person_external_id',
                input_keys=input_keys,
                new_ids=new_ids,
                observed_at=now,
            )
            if snap is None:
                snap = latest_snapshot_id(table)
        except (EmployeeLakeNotConfiguredError, EmployeeLakeWriteError):
            raise
        except Exception as exc:
            raise EmployeeLakeWriteError(f'raw.employees write failed: {exc}', cause=exc) from exc

        return MasterDataBatchResult(row_count=len(items), snapshot_id=snap, backend='iceberg')


# ---------------------------------------------------------------------------
# Arrow builder
# ---------------------------------------------------------------------------


def _build_arrow(
    items: list[EmployeeBulkItem],
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

    ids, person_ext_ids, org_unit_ext_ids = [], [], []
    is_lockeds, descriptions, attributes_jsons = [], [], []
    is_actives, tombstoned_ats, source_names = [], [], []
    ingest_batch_ids, observed_ats, ingested_ats = [], [], []

    for item in items:
        ids.append(new_row_id())
        person_ext_ids.append(item.person_external_id)
        org_unit_ext_ids.append(item.org_unit_external_id)
        is_lockeds.append(item.is_locked)
        descriptions.append(item.description)
        attributes_jsons.append(json.dumps(item.attributes, sort_keys=True) if item.attributes else None)
        is_actives.append(True)
        tombstoned_ats.append(None)
        source_names.append(source_name)
        ingest_batch_ids.append(batch_id_str)
        observed_ats.append(now_us)
        ingested_ats.append(now_us)

    return pa.table(
        {
            'id': pa.array(ids, type=pa.string()),
            'person_external_id': pa.array(person_ext_ids, type=pa.string()),
            'org_unit_external_id': pa.array(org_unit_ext_ids, type=pa.string()),
            'is_locked': pa.array(is_lockeds, type=pa.bool_()),
            'description': pa.array(descriptions, type=pa.string()),
            'attributes': pa.array(attributes_jsons, type=pa.string()),
            'is_active': pa.array(is_actives, type=pa.bool_()),
            'tombstoned_at': pa.array(tombstoned_ats, type=tz_us),
            'source_name': pa.array(source_names, type=pa.string()),
            'ingest_batch_id': pa.array(ingest_batch_ids, type=pa.string()),
            'observed_at': pa.array(observed_ats, type=tz_us),
            'ingested_at': pa.array(ingested_ats, type=tz_us),
        },
        schema=pa_schema,
    )

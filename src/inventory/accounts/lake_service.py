# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccountLakeService — writes raw.accounts to the Iceberg lake.

Append-only with retire-on-re-upload: when the same (application_id, username)
arrives in a new batch, the old active row is overwritten (is_active=False,
tombstoned_at) and the new row is the authoritative version.

No reads, no PG access.  Reconciliation reads from the lake and
applies to PG via master_data_pipeline + master_data_apply.
"""

from __future__ import annotations

from datetime import UTC, datetime
import json
import uuid

from pyiceberg.catalog import Catalog
from src.inventory.accounts.schemas import AccountBulkItem
from src.platform.lake.raw_master_writer import (
    MasterDataBatchResult,
    latest_snapshot_id,
    new_row_id,
    retire_and_commit,
    scan_active_by_keys,
    ts_micros,
)
from src.platform.lake.schemas import RAW_ACCOUNTS_TABLE


class AccountLakeNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__('AccountLakeService requires lake_catalog')


class AccountLakeWriteError(Exception):
    def __init__(self, message: str, *, cause: Exception) -> None:
        self.cause = cause
        super().__init__(message)


class AccountLakeService:
    """Writes AccountBulkItem batches to raw.accounts (Iceberg lake)."""

    def __init__(self, lake_catalog: Catalog | None = None) -> None:
        self._catalog = lake_catalog

    async def upsert_batch(
        self,
        items: list[AccountBulkItem],
        *,
        ingest_batch_id: uuid.UUID,
    ) -> MasterDataBatchResult:
        """Append accounts to lake; retire previous rows with same (application_id, username)."""
        if self._catalog is None:
            raise AccountLakeNotConfiguredError()

        try:
            table = self._catalog.load_table(RAW_ACCOUNTS_TABLE)
            pa_schema = table.schema().as_arrow()
            now = datetime.now(UTC)
            arrow_new = _build_arrow(items, pa_schema=pa_schema, now=now, ingest_batch_id=ingest_batch_id)
            table.append(arrow_new)

            # Retire old active rows for the same natural keys.
            # Natural key = composite (application_id, username); we encode as "app_id::username".
            input_keys = {_natural_key(item) for item in items}
            new_ids = set(arrow_new.column('id').to_pylist())
            scan = scan_active_by_keys(table, key_column='_natural_key_hint', keys=list(input_keys))
            snap = retire_and_commit(
                table,
                scan_arrow=scan,
                key_column='_natural_key_hint',
                input_keys=input_keys,
                new_ids=new_ids,
                observed_at=now,
            )
            if snap is None:
                snap = latest_snapshot_id(table)
        except (AccountLakeNotConfiguredError, AccountLakeWriteError):
            raise
        except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
            raise AccountLakeWriteError(f'raw.accounts write failed: {exc}', cause=exc) from exc

        return MasterDataBatchResult(row_count=len(items), snapshot_id=snap, backend='iceberg')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _natural_key(item: AccountBulkItem) -> str:
    """Composite natural key encoded as a single string for lake column lookup."""
    return f'{item.application_id}::{item.username}'


# ---------------------------------------------------------------------------
# Arrow builder
# ---------------------------------------------------------------------------


def _build_arrow(
    items: list[AccountBulkItem],
    *,
    pa_schema: object,
    now: datetime,
    ingest_batch_id: uuid.UUID,
) -> object:
    import pyarrow as pa  # noqa: PLC0415

    now_us = ts_micros(now)
    batch_id_str = str(ingest_batch_id)
    tz_us = pa.timestamp('us', tz='UTC')

    ids, ingest_batch_ids, application_ids, usernames = [], [], [], []
    external_ids, display_names, emails, statuses = [], [], [], []
    is_privilegeds, mfa_enableds, metas = [], [], []
    observed_ats, inserted_ats = [], []
    is_actives, tombstoned_ats = [], []
    natural_key_hints = []

    for item in items:
        ids.append(new_row_id())
        ingest_batch_ids.append(batch_id_str)
        application_ids.append(str(item.application_id))
        usernames.append(item.username)
        external_ids.append(item.external_id)
        display_names.append(item.display_name)
        emails.append(item.email)
        statuses.append(item.status.value if item.status is not None else None)
        is_privilegeds.append(item.is_privileged if item.is_privileged is not None else False)
        mfa_enableds.append(item.mfa_enabled if item.mfa_enabled is not None else False)
        metas.append(json.dumps(item.meta) if item.meta else None)
        observed_ats.append(now_us)
        inserted_ats.append(now_us)
        is_actives.append(True)
        tombstoned_ats.append(None)
        natural_key_hints.append(_natural_key(item))

    return pa.table(
        {
            'id': pa.array(ids, type=pa.string()),
            'ingest_batch_id': pa.array(ingest_batch_ids, type=pa.string()),
            'application_id': pa.array(application_ids, type=pa.string()),
            'username': pa.array(usernames, type=pa.string()),
            'external_id': pa.array(external_ids, type=pa.string()),
            'display_name': pa.array(display_names, type=pa.string()),
            'email': pa.array(emails, type=pa.string()),
            'status': pa.array(statuses, type=pa.string()),
            'is_privileged': pa.array(is_privilegeds, type=pa.bool_()),
            'mfa_enabled': pa.array(mfa_enableds, type=pa.bool_()),
            'meta': pa.array(metas, type=pa.string()),
            'observed_at': pa.array(observed_ats, type=tz_us),
            '_inserted_at': pa.array(inserted_ats, type=tz_us),
            'is_active': pa.array(is_actives, type=pa.bool_()),
            'tombstoned_at': pa.array(tombstoned_ats, type=tz_us),
            '_natural_key_hint': pa.array(natural_key_hints, type=pa.string()),
        },
        schema=pa_schema,
    )

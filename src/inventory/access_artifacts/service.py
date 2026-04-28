# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact service — lake-only read/write path.

Phase 15 Step 16: PG branch removed. Iceberg is the sole storage backend.
ORM imports removed. AccessArtifactView DTO returned from read methods.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import time
from typing import Any, Literal, NoReturn
import uuid

from pyiceberg.catalog import Catalog
from src.inventory.access_artifacts.schemas import AccessArtifactView
from src.platform.lake.config import LakeSettings
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import (
    LogService,
    NoOpLogService,
    merge_emit_log_participant_fields,
    noop_log_service,
)

_COMPONENT = 'inventory.access_artifacts'
_BATCH_SIZE_LIMIT = 10_000


# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class AccessArtifactApplicationNotFoundError(Exception):
    """Raised when the referenced application does not exist."""

    def __init__(self, application_id: uuid.UUID) -> None:
        self.application_id = application_id
        super().__init__(f'Application not found: {application_id}')


class AccessArtifactBatchTooLargeError(Exception):
    """Raised when the batch exceeds the maximum allowed size."""

    def __init__(self, count: int, limit: int) -> None:
        self.count = count
        self.limit = limit
        super().__init__(f'Batch size {count} exceeds limit {limit}')


class AccessArtifactLakeNotConfiguredError(Exception):
    """Raised when iceberg backend is requested but lake_catalog is not provided."""

    def __init__(self) -> None:
        super().__init__(
            'artifacts_write_backend=iceberg requires lake_catalog to be provided at service construction time'
        )


class AccessArtifactLakeWriteError(Exception):
    """Raised when an Iceberg write operation fails."""

    def __init__(self, message: str, *, cause: Exception) -> None:
        self.cause = cause
        super().__init__(message)


class InvalidCursorError(Exception):
    """Raised when a pagination cursor token is malformed or undecodable."""

    def __init__(self, detail: str = 'invalid cursor') -> None:
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactCursorPage:
    """Result of a cursor-paginated iceberg scan."""

    items: list[AccessArtifactView]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class BatchUpsertResult:
    """Result of a batch upsert operation."""

    row_count: int
    snapshot_id: int | None
    backend: Literal['iceberg']


@dataclass(frozen=True, slots=True)
class BatchTombstoneResult:
    """Result of a batch tombstone operation."""

    row_count: int
    snapshot_id: int | None
    backend: Literal['iceberg']


# ---------------------------------------------------------------------------
# Batch item input type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AccessArtifactBatchItem:
    """Input item for upsert_batch."""

    application_id: uuid.UUID
    artifact_type: str
    external_id: str
    payload: dict[str, Any]
    raw_name: str | None = None
    effect: str | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    observed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _ts_micros(value: datetime | None) -> int | None:
    """Convert a datetime to microseconds since epoch for PyArrow timestamp arrays."""
    if value is None:
        return None
    ts = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return int(ts.timestamp() * 1_000_000)


def _build_arrow_table_for_upsert(
    items: list[AccessArtifactBatchItem],
    *,
    ingest_batch_id: uuid.UUID,
    pa_schema: Any,
) -> Any:
    """Build a PyArrow table from the input items for Iceberg append."""
    import pyarrow as pa

    now = datetime.now(UTC)
    ts_type = pa.timestamp('us', tz='UTC')

    id_type = pa_schema.field('id').type
    app_id_type = pa_schema.field('application_id').type
    batch_id_type = pa_schema.field('ingest_batch_id').type

    use_uuid_bytes = hasattr(id_type, 'wrap_array')

    row_ids: list[Any] = []
    application_ids: list[Any] = []
    artifact_types: list[str] = []
    external_ids: list[str] = []
    payloads: list[str | None] = []
    raw_names: list[str | None] = []
    effects: list[str | None] = []
    valid_froms: list[int | None] = []
    valid_untils: list[int | None] = []
    is_actives: list[bool] = []
    tombstoned_ats: list[int | None] = []
    observed_ats: list[int | None] = []
    ingested_ats: list[int | None] = []
    ingest_batch_ids: list[Any] = []

    now_us = _ts_micros(now)
    batch_id_val: Any = ingest_batch_id.bytes if use_uuid_bytes else str(ingest_batch_id)

    for item in items:
        new_id = uuid.uuid4()
        row_ids.append(new_id.bytes if use_uuid_bytes else str(new_id))
        application_ids.append(item.application_id.bytes if use_uuid_bytes else str(item.application_id))
        artifact_types.append(item.artifact_type)
        external_ids.append(item.external_id)
        payloads.append(json.dumps(item.payload, sort_keys=True) if item.payload is not None else None)
        raw_names.append(item.raw_name)
        effects.append(item.effect)
        valid_froms.append(_ts_micros(item.valid_from))
        valid_untils.append(_ts_micros(item.valid_until))
        is_actives.append(True)
        tombstoned_ats.append(None)
        observed_ats.append(_ts_micros(item.observed_at) if item.observed_at is not None else now_us)
        ingested_ats.append(now_us)
        ingest_batch_ids.append(batch_id_val)

    if use_uuid_bytes:
        id_arr: Any = id_type.wrap_array(pa.array(row_ids, type=pa.binary(16)))
        app_id_arr: Any = app_id_type.wrap_array(pa.array(application_ids, type=pa.binary(16)))
        batch_id_arr: Any = batch_id_type.wrap_array(pa.array(ingest_batch_ids, type=pa.binary(16)))
    else:
        id_arr = pa.array(row_ids, type=id_type)
        app_id_arr = pa.array(application_ids, type=app_id_type)
        batch_id_arr = pa.array(ingest_batch_ids, type=batch_id_type)

    raw: dict[str, Any] = {
        'id': id_arr,
        'application_id': app_id_arr,
        'artifact_type': pa.array(artifact_types),
        'external_id': pa.array(external_ids),
        'payload': pa.array(payloads, type=pa.string()),
        'raw_name': pa.array(raw_names, type=pa.string()),
        'effect': pa.array(effects, type=pa.string()),
        'valid_from': pa.array(valid_froms, type=ts_type),
        'valid_until': pa.array(valid_untils, type=ts_type),
        'is_active': pa.array(is_actives, type=pa.bool_()),
        'tombstoned_at': pa.array(tombstoned_ats, type=ts_type),
        'observed_at': pa.array(observed_ats, type=ts_type),
        'ingested_at': pa.array(ingested_ats, type=ts_type),
        'ingest_batch_id': batch_id_arr,
    }

    return pa.table(raw, schema=pa_schema)


def _extract_id_value(id_scalar: Any) -> Any:
    """Extract a comparable ID value from a PyArrow scalar."""
    return id_scalar.as_py()


def _compute_retired_rows(
    scan_arrow: Any,
    *,
    input_keys: set[tuple[str, str]],
    new_ids: set[Any],
    observed_at: datetime,
) -> Any | None:
    """Identify existing active rows that are superseded by the new batch."""
    import pyarrow as pa

    if len(scan_arrow) == 0:
        return None

    id_col = scan_arrow.column('id')
    is_active_col = scan_arrow.column('is_active')
    external_id_col = scan_arrow.column('external_id')
    application_id_col = scan_arrow.column('application_id')

    retiree_indices = []
    for i in range(len(scan_arrow)):
        row_active = is_active_col[i].as_py()
        if not row_active:
            continue
        row_id = _extract_id_value(id_col[i])
        if row_id in new_ids:
            continue
        row_external_id = external_id_col[i].as_py()
        row_app_id_raw = _extract_id_value(application_id_col[i])
        row_app_id_str = str(row_app_id_raw)
        if (row_external_id, row_app_id_str) in input_keys:
            retiree_indices.append(i)

    if not retiree_indices:
        return None

    retirees = scan_arrow.take(retiree_indices)

    tz_us = pa.timestamp('us', tz='UTC')
    ts_val = _ts_micros(observed_at)
    tombstoned_array = pa.array([ts_val] * len(retirees), type=tz_us)
    is_active_false = pa.array([False] * len(retirees), type=pa.bool_())

    col_names = retirees.column_names
    new_cols = []
    for name in col_names:
        if name == 'is_active':
            new_cols.append(is_active_false)
        elif name == 'tombstoned_at':
            new_cols.append(tombstoned_array)
        else:
            new_cols.append(retirees.column(name))

    return pa.table(dict(zip(col_names, new_cols, strict=True)), schema=retirees.schema)


def _scan_active_partition(
    table: Any,
    *,
    app_id_val: Any,
    artifact_type: str,
) -> Any:
    """Scan Iceberg table for active rows in a given partition."""
    from pyiceberg.expressions import And, EqualTo  # noqa: PLC0415

    eq_app: Any = EqualTo('application_id', app_id_val)  # type: ignore[misc, arg-type, call-arg]
    eq_type: Any = EqualTo('artifact_type', artifact_type)  # type: ignore[misc, arg-type, call-arg]
    eq_active: Any = EqualTo('is_active', True)  # type: ignore[misc, arg-type, call-arg]
    inner_and: Any = And(eq_type, eq_active)
    row_filter: Any = And(eq_app, inner_and)
    return table.scan(row_filter=row_filter).to_arrow()


def _build_id_filter(ids: list[Any]) -> Any:
    """Build an Iceberg ``In`` filter for the ``id`` column."""
    from pyiceberg.expressions import In  # noqa: PLC0415

    return In('id', ids)  # type: ignore[misc, arg-type, call-arg]


def _translate_lake_write_error(
    exc: Exception,
    *,
    backend: str,
    operation: str,
    ingest_batch_id: uuid.UUID | None,
    log_service: LogService | NoOpLogService,
) -> NoReturn:
    """Wrap any Iceberg / PyArrow exception into AccessArtifactLakeWriteError."""
    payload: dict[str, Any] = {
        'backend': backend,
        'operation': operation,
        'error_type': type(exc).__name__,
        'error_message': str(exc),
    }
    if ingest_batch_id is not None:
        payload['ingest_batch_id'] = str(ingest_batch_id)

    log_service.emit_safe(
        level=LogLevel.ERROR,
        message='inventory.access_artifacts.batch_write_failed',
        component=_COMPONENT,
        payload=merge_emit_log_participant_fields(
            payload,
            actor_component=_COMPONENT,
            target_id='batch',
        ),
    )
    raise AccessArtifactLakeWriteError(
        f'Lake write failed [{operation}]: {exc}',
        cause=exc,
    ) from exc


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _encode_cursor(last_seen_id: str) -> str:
    """Encode last_seen_id into an opaque base64url cursor token."""
    token = json.dumps({'last_seen_id': last_seen_id}, separators=(',', ':'))
    return base64.urlsafe_b64encode(token.encode()).decode().rstrip('=')


def _decode_cursor(cursor: str) -> str:
    """Decode a base64url cursor token and return last_seen_id."""
    try:
        padded = cursor + '=' * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        data = json.loads(raw)
        last_seen_id = data['last_seen_id']
        if not isinstance(last_seen_id, str):
            raise InvalidCursorError()
        return last_seen_id
    except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError() from exc


_SCAN_COLUMNS = (
    'id',
    'application_id',
    'artifact_type',
    'external_id',
    'payload',
    'raw_name',
    'effect',
    'valid_from',
    'valid_until',
    'ingested_at',
    'ingest_batch_id',
    'observed_at',
    'is_active',
    'tombstoned_at',
)


def _row_to_view(row: tuple[Any, ...]) -> AccessArtifactView:
    """Convert a DuckDB result row (matching _SCAN_COLUMNS) to AccessArtifactView."""
    d = dict(zip(_SCAN_COLUMNS, row, strict=True))
    if isinstance(d.get('payload'), str):
        try:
            d['payload'] = json.loads(d['payload'])
        except (ValueError, TypeError):
            d['payload'] = {}
    elif d.get('payload') is None:
        d['payload'] = {}
    # Cast ingest_batch_id to str if it's a UUID object
    if d.get('ingest_batch_id') is not None and not isinstance(d['ingest_batch_id'], str):
        d['ingest_batch_id'] = str(d['ingest_batch_id'])
    return AccessArtifactView.model_validate(d, strict=False)


def _run_iceberg_scan(
    lake_session: Any,
    *,
    warehouse_uri: str,
    application_id: uuid.UUID | None,
    artifact_type: str | None,
    is_active: bool | None,
    last_seen_id: str | None,
    fetch_size: int,
) -> list[AccessArtifactView]:
    """Execute DuckDB iceberg_scan query. Blocking — must be called via asyncio.to_thread."""
    predicates: list[str] = []
    params: list[Any] = []

    if application_id is not None:
        predicates.append('application_id = ?')
        params.append(str(application_id))

    if last_seen_id is not None:
        predicates.append('id > ?')
        params.append(last_seen_id)

    if is_active is not None:
        predicates.append('is_active = ?')
        params.append(is_active)

    if artifact_type is not None:
        predicates.append('artifact_type = ?')
        params.append(artifact_type)

    where_clause = ('WHERE ' + ' AND '.join(predicates)) if predicates else ''

    sql = f"""
        SELECT
            id, application_id, artifact_type, external_id,
            payload, raw_name, effect, valid_from, valid_until,
            ingested_at, ingest_batch_id, observed_at,
            is_active, tombstoned_at
        FROM iceberg_scan('{warehouse_uri}/raw/access_artifacts', skip_schema_inference=true)
        {where_clause}
        ORDER BY id
        LIMIT ?
    """
    params.append(fetch_size)

    lake_session.execute(sql, params)
    rows_raw: list[Any] = lake_session.fetchall()

    return [_row_to_view(row) for row in rows_raw]


def _run_get_by_id(
    lake_session: Any,
    *,
    warehouse_uri: str,
    artifact_id: uuid.UUID,
) -> AccessArtifactView | None:
    """Fetch single artifact by id via DuckDB. Blocking."""
    sql = f"""
        SELECT
            id, application_id, artifact_type, external_id,
            payload, raw_name, effect, valid_from, valid_until,
            ingested_at, ingest_batch_id, observed_at,
            is_active, tombstoned_at
        FROM iceberg_scan('{warehouse_uri}/raw/access_artifacts', skip_schema_inference=true)
        WHERE id = ?::uuid
        LIMIT 1
    """
    lake_session.execute(sql, [str(artifact_id)])
    rows = lake_session.fetchmany(1)
    if not rows:
        return None
    return _row_to_view(rows[0])


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AccessArtifactService:
    """Orchestrates access artifact upsert and retrieval via Iceberg lake."""

    def __init__(
        self,
        log_service: LogService | None = None,
        lake_settings: LakeSettings | None = None,
        lake_catalog: Catalog | None = None,
    ) -> None:
        self._log: LogService | NoOpLogService = log_service if log_service is not None else noop_log_service
        self._lake_settings = lake_settings
        self._lake_catalog = lake_catalog

    def _resolve_backend_public(self) -> Literal['iceberg']:
        """Expose backend — always iceberg (Step 16)."""
        return 'iceberg'

    def _get_warehouse_uri(self) -> str:
        """Return warehouse URI from lake_settings, defaulting to empty string."""
        if self._lake_settings is None:
            return ''
        return self._lake_settings.warehouse_uri

    def _get_read_page_size(self) -> int:
        """Return read_page_size from lake_settings, defaulting to 1000."""
        if self._lake_settings is None:
            return 1000
        return self._lake_settings.read_page_size

    async def upsert_artifact(self, *args: Any, **kwargs: Any) -> Any:
        """Removed in Phase 15 Step 16. Use upsert_batch instead.

        Stub for backward compat with normalization/acl callers.
        Raises NotImplementedError at runtime.
        """
        raise NotImplementedError(
            'AccessArtifactService.upsert_artifact was removed in Phase 15 Step 16. Use upsert_batch (Iceberg) instead.'
        )

    async def tombstone_artifact(self, *args: Any, **kwargs: Any) -> Any:
        """Removed in Phase 15 Step 16. Use tombstone_batch instead."""
        raise NotImplementedError('AccessArtifactService.tombstone_artifact was removed in Phase 15 Step 16.')

    async def list_artifacts(self, *args: Any, **kwargs: Any) -> Any:
        """Removed in Phase 15 Step 16. Use list_artifacts_iceberg instead."""
        raise NotImplementedError('AccessArtifactService.list_artifacts was removed in Phase 15 Step 16.')

    async def get_artifact(
        self,
        lake_session: Any,
        artifact_id: uuid.UUID,
    ) -> AccessArtifactView | None:
        """Get access artifact by id via DuckDB iceberg_scan. Returns DTO or None."""
        warehouse_uri = self._get_warehouse_uri()
        start_ms = time.monotonic() * 1000

        view = await asyncio.to_thread(
            _run_get_by_id,
            lake_session,
            warehouse_uri=warehouse_uri,
            artifact_id=artifact_id,
        )

        duration_ms = int(time.monotonic() * 1000 - start_ms)
        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_artifacts.get_by_id_completed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'artifact_id': str(artifact_id),
                    'found': view is not None,
                    'duration_ms': duration_ms,
                },
                actor_component=_COMPONENT,
                target_id=str(artifact_id),
            ),
        )
        return view

    async def list_artifacts_iceberg(
        self,
        lake_session: Any,
        *,
        warehouse_uri: str,
        application_id: uuid.UUID | None = None,
        artifact_type: str | None = None,
        is_active: bool | None = None,
        cursor: str | None = None,
        page_size: int = 1000,
    ) -> ArtifactCursorPage:
        """List access artifacts via DuckDB iceberg_scan with cursor pagination."""
        last_seen_id: str | None = None

        if cursor is not None:
            last_seen_id = _decode_cursor(cursor)

        page_size_clamped = min(max(page_size, 1), 5000)
        fetch_size = page_size_clamped + 1  # one extra to detect "more"

        start_ms = time.monotonic() * 1000

        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_artifacts.iceberg_scan_started',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'warehouse_uri': warehouse_uri,
                    'application_id': str(application_id) if application_id is not None else None,
                    'artifact_type': artifact_type,
                    'is_active': is_active,
                    'cursor_present': cursor is not None,
                    'page_size': page_size_clamped,
                },
                actor_component=_COMPONENT,
                target_id='iceberg_scan',
            ),
        )

        views = await asyncio.to_thread(
            _run_iceberg_scan,
            lake_session,
            warehouse_uri=warehouse_uri,
            application_id=application_id,
            artifact_type=artifact_type,
            is_active=is_active,
            last_seen_id=last_seen_id,
            fetch_size=fetch_size,
        )

        duration_ms = int(time.monotonic() * 1000 - start_ms)

        has_more = len(views) == fetch_size
        if has_more:
            views = views[:-1]

        next_cursor: str | None = None
        if has_more and views:
            next_cursor = _encode_cursor(str(views[-1].id))

        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_artifacts.iceberg_scan_completed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'row_count': len(views),
                    'has_more': has_more,
                    'duration_ms': duration_ms,
                },
                actor_component=_COMPONENT,
                target_id='iceberg_scan',
            ),
        )

        return ArtifactCursorPage(items=views, next_cursor=next_cursor)

    async def upsert_batch(
        self,
        items: list[AccessArtifactBatchItem],
        *,
        ingest_batch_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> BatchUpsertResult:
        """Batch upsert of access artifacts via Iceberg. PG path removed in Step 16.

        Raises:
            AccessArtifactBatchTooLargeError: when len(items) > 10_000.
            AccessArtifactLakeNotConfiguredError: when lake_catalog is not provided.
            AccessArtifactLakeWriteError: when the Iceberg write fails.
        """
        if len(items) > _BATCH_SIZE_LIMIT:
            raise AccessArtifactBatchTooLargeError(len(items), _BATCH_SIZE_LIMIT)

        if self._lake_catalog is None:
            raise AccessArtifactLakeNotConfiguredError()

        self._log.emit_safe(
            level=LogLevel.INFO,
            message='inventory.access_artifacts.batch_upsert_started',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'row_count': len(items),
                    'backend': 'iceberg',
                    'ingest_batch_id': str(ingest_batch_id),
                },
                actor_component=_COMPONENT,
                target_id='batch',
            ),
        )

        start_ms = time.monotonic() * 1000
        result = await self._upsert_batch_iceberg(items, ingest_batch_id=ingest_batch_id)
        duration_ms = int(time.monotonic() * 1000 - start_ms)

        self._log.emit_safe(
            level=LogLevel.INFO,
            message='inventory.access_artifacts.batch_upsert_completed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'row_count': result.row_count,
                    'backend': 'iceberg',
                    'snapshot_id': result.snapshot_id,
                    'ingest_batch_id': str(ingest_batch_id),
                    'duration_ms': duration_ms,
                },
                actor_component=_COMPONENT,
                target_id='batch',
            ),
        )

        return result

    async def tombstone_batch(
        self,
        artifact_ids: list[uuid.UUID],
        *,
        observed_at: datetime,
        correlation_id: str | None = None,
    ) -> BatchTombstoneResult:
        """Batch tombstone of access artifacts via Iceberg. PG path removed in Step 16.

        Raises:
            AccessArtifactBatchTooLargeError: when len(artifact_ids) > 10_000.
            AccessArtifactLakeNotConfiguredError: when lake_catalog is not provided.
            AccessArtifactLakeWriteError: when the Iceberg write fails.
        """
        if len(artifact_ids) > _BATCH_SIZE_LIMIT:
            raise AccessArtifactBatchTooLargeError(len(artifact_ids), _BATCH_SIZE_LIMIT)

        if self._lake_catalog is None:
            raise AccessArtifactLakeNotConfiguredError()

        self._log.emit_safe(
            level=LogLevel.INFO,
            message='inventory.access_artifacts.batch_tombstone_started',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'row_count': len(artifact_ids),
                    'backend': 'iceberg',
                },
                actor_component=_COMPONENT,
                target_id='batch',
            ),
        )

        result = await self._tombstone_batch_iceberg(artifact_ids, observed_at=observed_at)

        self._log.emit_safe(
            level=LogLevel.INFO,
            message='inventory.access_artifacts.batch_tombstone_completed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'row_count': result.row_count,
                    'backend': 'iceberg',
                    'snapshot_id': result.snapshot_id,
                },
                actor_component=_COMPONENT,
                target_id='batch',
            ),
        )

        return result

    # ------------------------------------------------------------------
    # Private: iceberg path
    # ------------------------------------------------------------------

    async def _upsert_batch_iceberg(
        self,
        items: list[AccessArtifactBatchItem],
        *,
        ingest_batch_id: uuid.UUID,
    ) -> BatchUpsertResult:
        """Write batch to Iceberg using two-snapshot dedup sequence."""
        from src.platform.lake.schemas import RAW_ACCESS_ARTIFACTS_TABLE

        assert self._lake_catalog is not None  # guarded in upsert_batch

        try:
            table = self._lake_catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
        except Exception as exc:
            _translate_lake_write_error(
                exc,
                backend='iceberg',
                operation='load_table',
                ingest_batch_id=ingest_batch_id,
                log_service=self._log,
            )

        pa_schema = table.schema().as_arrow()

        try:
            arrow_new = _build_arrow_table_for_upsert(items, ingest_batch_id=ingest_batch_id, pa_schema=pa_schema)
        except Exception as exc:
            _translate_lake_write_error(
                exc,
                backend='iceberg',
                operation='build_arrow_table',
                ingest_batch_id=ingest_batch_id,
                log_service=self._log,
            )

        try:
            table.append(arrow_new)
        except Exception as exc:
            _translate_lake_write_error(
                exc,
                backend='iceberg',
                operation='append',
                ingest_batch_id=ingest_batch_id,
                log_service=self._log,
            )

        new_ids: set[Any] = set()
        id_col = arrow_new.column('id')
        for i in range(len(arrow_new)):
            new_ids.add(_extract_id_value(id_col[i]))

        partitions: list[tuple[Any, str]] = []
        seen_partitions: set[tuple[str, str]] = set()
        app_id_col = arrow_new.column('application_id')
        artifact_type_col = arrow_new.column('artifact_type')
        for i in range(len(arrow_new)):
            app_id_val = _extract_id_value(app_id_col[i])
            atype: str = artifact_type_col[i].as_py()
            app_id_str = str(app_id_val)
            key = (app_id_str, atype)
            if key not in seen_partitions:
                seen_partitions.add(key)
                partitions.append((app_id_val, atype))

        observed_at = datetime.now(UTC)

        input_keys: set[tuple[str, str]] = set()
        ext_id_col = arrow_new.column('external_id')
        for i in range(len(arrow_new)):
            app_id_val_i = _extract_id_value(app_id_col[i])
            ext_id: str = ext_id_col[i].as_py()
            input_keys.add((ext_id, str(app_id_val_i)))

        latest_snapshot_id: int | None = None
        try:
            latest_snapshot_id = table.metadata.current_snapshot_id
        except Exception:
            pass

        for app_id_val, atype in partitions:
            app_id_str = str(app_id_val)
            try:
                scan_arrow = _scan_active_partition(table, app_id_val=app_id_val, artifact_type=atype)
            except Exception as exc:
                _translate_lake_write_error(
                    exc,
                    backend='iceberg',
                    operation='scan_partition',
                    ingest_batch_id=ingest_batch_id,
                    log_service=self._log,
                )

            partition_input_keys = {(ext, aid) for (ext, aid) in input_keys if aid == app_id_str}

            retired = _compute_retired_rows(
                scan_arrow,
                input_keys=partition_input_keys,
                new_ids=new_ids,
                observed_at=observed_at,
            )
            if retired is None or len(retired) == 0:
                continue

            retiree_ids: list[Any] = []
            ret_id_col = retired.column('id')
            for i in range(len(retired)):
                retiree_ids.append(_extract_id_value(ret_id_col[i]))

            try:
                table.overwrite(
                    retired,
                    overwrite_filter=_build_id_filter(retiree_ids),
                )
                latest_snapshot_id = table.metadata.current_snapshot_id
            except Exception as exc:
                _translate_lake_write_error(
                    exc,
                    backend='iceberg',
                    operation='overwrite_retire',
                    ingest_batch_id=ingest_batch_id,
                    log_service=self._log,
                )

        if latest_snapshot_id is None:
            try:
                latest_snapshot_id = table.metadata.current_snapshot_id
            except Exception:
                pass

        return BatchUpsertResult(
            row_count=len(items),
            snapshot_id=latest_snapshot_id,
            backend='iceberg',
        )

    async def _tombstone_batch_iceberg(
        self,
        artifact_ids: list[uuid.UUID],
        *,
        observed_at: datetime,
    ) -> BatchTombstoneResult:
        """Partition-level read-modify-write tombstone via Iceberg."""
        from src.platform.lake.schemas import RAW_ACCESS_ARTIFACTS_TABLE

        assert self._lake_catalog is not None  # guarded in tombstone_batch

        try:
            table = self._lake_catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
        except Exception as exc:
            _translate_lake_write_error(
                exc,
                backend='iceberg',
                operation='load_table_tombstone',
                ingest_batch_id=None,
                log_service=self._log,
            )

        pa_schema = table.schema().as_arrow()
        id_type = pa_schema.field('id').type
        use_uuid_bytes = hasattr(id_type, 'wrap_array')

        if use_uuid_bytes:
            id_filter_values: list[Any] = [aid.bytes for aid in artifact_ids]
            id_values_set: set[Any] = {uuid.UUID(bytes=b) for b in id_filter_values}
        else:
            id_filter_values = [str(aid) for aid in artifact_ids]
            id_values_set = set(id_filter_values)

        try:
            scan_arrow = table.scan(
                row_filter=_build_id_filter(id_filter_values),
            ).to_arrow()
        except Exception as exc:
            _translate_lake_write_error(
                exc,
                backend='iceberg',
                operation='scan_tombstone',
                ingest_batch_id=None,
                log_service=self._log,
            )

        if len(scan_arrow) == 0:
            return BatchTombstoneResult(row_count=0, snapshot_id=None, backend='iceberg')

        import pyarrow as pa

        tz_us = pa.timestamp('us', tz='UTC')
        ts_val = _ts_micros(observed_at)

        id_col = scan_arrow.column('id')
        match_mask = []
        for i in range(len(scan_arrow)):
            row_id = _extract_id_value(id_col[i])
            match_mask.append(row_id in id_values_set)

        tombstoned_vals = [ts_val if match_mask[i] else None for i in range(len(scan_arrow))]
        is_active_col_src = scan_arrow.column('is_active')
        is_active_vals = [False if match_mask[i] else is_active_col_src[i].as_py() for i in range(len(scan_arrow))]

        col_names = scan_arrow.column_names
        new_cols = []
        for name in col_names:
            if name == 'is_active':
                new_cols.append(pa.array(is_active_vals, type=pa.bool_()))
            elif name == 'tombstoned_at':
                new_cols.append(pa.array(tombstoned_vals, type=tz_us))
            else:
                new_cols.append(scan_arrow.column(name))

        modified_arrow = pa.table(dict(zip(col_names, new_cols, strict=True)), schema=scan_arrow.schema)

        try:
            table.overwrite(
                modified_arrow,
                overwrite_filter=_build_id_filter(id_filter_values),
            )
        except Exception as exc:
            _translate_lake_write_error(
                exc,
                backend='iceberg',
                operation='overwrite_tombstone',
                ingest_batch_id=None,
                log_service=self._log,
            )

        snapshot_id: int | None = None
        try:
            snapshot_id = table.metadata.current_snapshot_id
        except Exception:
            pass

        return BatchTombstoneResult(
            row_count=len(artifact_ids),
            snapshot_id=snapshot_id,
            backend='iceberg',
        )

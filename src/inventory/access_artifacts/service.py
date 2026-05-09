# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessArtifact service — thin domain-facing façade.

Phase 17 Step 18: Physical Iceberg / DuckDB / PyArrow I/O extracted to
``src/platform/lake/access_artifacts_writer.py`` and
``src/platform/lake/access_artifacts_reader.py``.

This module is a thin domain-facing façade: validates inputs, emits
observability emit_safe lines, delegates all physical I/O to platform/lake.
Public Python API (signatures) is byte-identical to before the refactor.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
import time
from typing import Any, Literal
import uuid

from src.inventory.access_artifacts.schemas import AccessArtifactView
from src.platform.events.service import EventService, NoOpEventService, noop_event_service

# ---------------------------------------------------------------------------
# Re-export shims — preserves "from src.inventory.access_artifacts.service import …"
# for external callers (ingest engine, tests). One-phase accommodation per TASK §2.5.
# ---------------------------------------------------------------------------
# InvalidCursorError re-exported from reader
from src.platform.lake.access_artifacts_reader import (
    SCAN_COLUMNS,
    AccessArtifactRow,
    InvalidCursorError,  # noqa: E402
    decode_cursor,
    encode_cursor,
    run_get_by_id,
    run_iceberg_scan,
)
from src.platform.lake.access_artifacts_writer import (
    AccessArtifactBatchItem,
    AccessArtifactLakeWriteError,
    BatchTombstoneResult,
    BatchUpsertResult,
    tombstone_batch_iceberg,
    upsert_batch_iceberg,
)
from src.platform.lake.config import LakeSettings
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import (
    LogService,
    NoOpLogService,
    merge_emit_log_participant_fields,
    noop_log_service,
)

# _encode_cursor shim — test_routes_bulk.py:27 imports the underscore name
_encode_cursor = encode_cursor

__all__ = [
    'AccessArtifactService',
    'AccessArtifactBatchItem',
    'AccessArtifactBatchTooLargeError',
    'AccessArtifactLakeNotConfiguredError',
    'AccessArtifactLakeWriteError',
    'BatchUpsertResult',
    'BatchTombstoneResult',
    'ArtifactCursorPage',
    'InvalidCursorError',
    '_encode_cursor',
]

_COMPONENT = 'inventory.access_artifacts'
_BATCH_SIZE_LIMIT = 10_000


# ---------------------------------------------------------------------------
# Domain errors (façade-level — validation / config, never raised by platform modules)
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


# ---------------------------------------------------------------------------
# DTO mapper — converts lake-level AccessArtifactRow → domain AccessArtifactView
# Lives in the façade to preserve the platform → inventory layer direction.
# ---------------------------------------------------------------------------


def _row_to_view(row: AccessArtifactRow) -> AccessArtifactView:
    """Convert a :class:`AccessArtifactRow` (lake boundary) to :class:`AccessArtifactView` (domain DTO)."""
    d: dict[str, Any] = {col: getattr(row, col) for col in SCAN_COLUMNS}
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


# ---------------------------------------------------------------------------
# Result type (façade-level — wraps list[AccessArtifactView], stays above platform layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ArtifactCursorPage:
    """Result of a cursor-paginated iceberg scan."""

    items: list[AccessArtifactView]
    next_cursor: str | None


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AccessArtifactService:
    """Thin domain-facing façade — orchestrates access artifact upsert and retrieval.

    Physical Iceberg / DuckDB / PyArrow I/O lives in platform/lake/access_artifacts_*.
    This class owns input validation, observability emit_safe lines, and result shaping.
    """

    def __init__(
        self,
        log_service: LogService | None = None,
        lake_settings: LakeSettings | None = None,
        lake_catalog: Any | None = None,
        event_service: EventService | None = None,
    ) -> None:
        self._log: LogService | NoOpLogService = log_service if log_service is not None else noop_log_service
        self._lake_settings = lake_settings
        self._lake_catalog = lake_catalog
        self._events: EventService | NoOpEventService = (
            event_service if event_service is not None else noop_event_service
        )

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

        row = await asyncio.to_thread(
            run_get_by_id,
            lake_session,
            warehouse_uri=warehouse_uri,
            artifact_id=artifact_id,
        )
        view = _row_to_view(row) if row is not None else None

        duration_ms = int(time.monotonic() * 1000 - start_ms)
        # allowed-emit-safe: observability
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
            last_seen_id = decode_cursor(cursor)

        page_size_clamped = min(max(page_size, 1), 5000)
        fetch_size = page_size_clamped + 1  # one extra to detect "more"

        start_ms = time.monotonic() * 1000

        # allowed-emit-safe: observability
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

        rows = await asyncio.to_thread(
            run_iceberg_scan,
            lake_session,
            warehouse_uri=warehouse_uri,
            application_id=application_id,
            artifact_type=artifact_type,
            is_active=is_active,
            last_seen_id=last_seen_id,
            fetch_size=fetch_size,
        )

        duration_ms = int(time.monotonic() * 1000 - start_ms)

        has_more = len(rows) == fetch_size
        if has_more:
            rows = rows[:-1]

        views = [_row_to_view(row) for row in rows]

        next_cursor: str | None = None
        if has_more and views:
            next_cursor = encode_cursor(str(views[-1].id))

        # allowed-emit-safe: observability
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

        # allowed-emit-safe: observability
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
        result = await upsert_batch_iceberg(
            items,
            ingest_batch_id=ingest_batch_id,
            catalog=self._lake_catalog,
            log_service=self._log,
        )
        duration_ms = int(time.monotonic() * 1000 - start_ms)

        # allowed-emit-safe: observability
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

        # allowed-emit-safe: observability
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

        result = await tombstone_batch_iceberg(
            artifact_ids,
            observed_at=observed_at,
            catalog=self._lake_catalog,
            log_service=self._log,
        )

        # allowed-emit-safe: observability
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

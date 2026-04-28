# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""LakeMigrationService — PG → Iceberg one-shot data migration.

Architecture constraints (ARCH_CONTEXT alignment):
- Engine slice; allowed imports: platform/lake, platform/logs, inventory/access_artifacts.models,
  inventory/access_facts.models, inventory/lake_batches.service,
  capabilities/reconciliation (models + hashing). ZERO imports from iga/, idp/, sync_apply.
- ZERO domain events (inventory.access_fact.* events belong exclusively to sync_apply/service.py).
- Service flushes; route commits.
- Advisory lock per dataset prevents parallel runs.
- BackgroundTasks is sufficient for current scope (~10M rows upper bound).

Upper bound: single-process BackgroundTasks can safely carry multi-million-row
migrations with streaming cursor (yield_per) + 5000-row batch commits + checkpoint
cursor for resume. Operator documentation note: ceiling ~10M rows total; beyond
that use an out-of-process worker (deferred phase).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.lake_migration.exceptions import (
    LakeMigrationConflictError,
    LakeMigrationDatasetError,
    LakeMigrationNotFoundError,
    LakeMigrationResumeError,
)
from src.capabilities.lake_migration.migration_writer import (
    append_artifact_batch,
    append_fact_batch,
    build_artifact_arrow_table,
    build_fact_arrow_table,
)
from src.capabilities.lake_migration.models import (
    LakeMigrationDataset,
    LakeMigrationRun,
    LakeMigrationStatus,
)
from src.capabilities.reconciliation.hashing import compute_natural_key_hash
from src.capabilities.reconciliation.models import (
    ReconciliationDeltaItem,
    ReconciliationDeltaItemStatus,
    ReconciliationDeltaOperation,
    ReconciliationRun,
    ReconciliationRunStatus,
)
from src.inventory.lake_batches.service import LakeBatchService
from src.platform.lake.schemas import (
    NORMALIZED_ACCESS_FACTS_TABLE,
    RAW_ACCESS_ARTIFACTS_TABLE,
)
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, NoOpLogService, merge_emit_capability_trace_fields

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog
    from src.platform.lake.duckdb_session import LakeSession

_CAPABILITY_ID = 'capabilities.lake_migration'
_COMPONENT = 'capabilities.lake_migration'

# Resumable statuses: a run in one of these states may be resumed.
_RESUMABLE_STATUSES = frozenset(
    {
        LakeMigrationStatus.failed,
        LakeMigrationStatus.cancelled,
        LakeMigrationStatus.pending,
        LakeMigrationStatus.running,
    }
)


def _advisory_lock_key(dataset: LakeMigrationDataset) -> int:
    """Return a stable int key for pg_try_advisory_xact_lock per dataset."""
    return hash(('lake_migration', dataset.value)) & 0x7FFFFFFFFFFFFFFF


class LakeMigrationService:
    """Orchestrates PG → Iceberg migration for access_artifacts and access_facts.

    Public methods:
    - ``start_migration``      — acquire advisory lock, create/resume run row.
    - ``migrate_access_artifacts`` — stream PG artifacts to Iceberg.
    - ``migrate_access_facts``  — stream PG facts to Iceberg + synthetic delta items.
    - ``get_run``               — load by id.
    - ``list_runs``             — cursor-paginated list.
    """

    def __init__(
        self,
        log_service: LogService | NoOpLogService,
        lake_batch_service: LakeBatchService,
    ) -> None:
        self._log = log_service
        self._lake_batches = lake_batch_service

    # ------------------------------------------------------------------
    # Start / resume
    # ------------------------------------------------------------------

    async def start_migration(
        self,
        session: AsyncSession,
        *,
        dataset: LakeMigrationDataset,
        batch_size: int = 5000,
        resume: uuid.UUID | None = None,
        correlation_id: str | None = None,
    ) -> LakeMigrationRun:
        """Create or resume a migration run.

        Acquires ``pg_try_advisory_xact_lock`` keyed on ``(lake_migration, dataset)``.
        Lock is held in the same DB transaction that creates the run row (released
        on caller commit).

        On lock conflict → ``LakeMigrationConflictError`` (409).

        Resume path: load existing row, verify dataset matches (else 422), verify
        status in resumable set (else 409), reset ``status='running'``,
        ``started_at=now()``, keep ``last_processed_id`` + ``lake_batch_id`` +
        ``synthetic_run_id``.

        Service flushes; route commits.
        """
        # Advisory lock — prevents parallel runs on same dataset.
        lock_key = _advisory_lock_key(dataset)
        result = await session.execute(text('SELECT pg_try_advisory_xact_lock(:key)'), {'key': lock_key})
        acquired: bool = result.scalar_one()
        if not acquired:
            raise LakeMigrationConflictError(dataset.value)

        if resume is not None:
            return await self._resume_run(session, resume_id=resume, dataset=dataset, correlation_id=correlation_id)

        # Create a new lake_batch row for provenance.
        lake_batch = await self._lake_batches.record_lake_write(
            session,
            dataset_type=f'pg_migration.{dataset.value}',
            iceberg_namespace='raw' if dataset == LakeMigrationDataset.access_artifacts else 'normalized',
            iceberg_table=dataset.value,
            snapshot_id=0,  # updated as batches commit
            row_count=0,
            metadata_json={'origin': 'pg_migration', 'dataset': dataset.value},
        )

        run = LakeMigrationRun(
            dataset=dataset,
            status=LakeMigrationStatus.pending,
            batch_size=batch_size,
            lake_batch_id=lake_batch.id,
            metadata_json={'origin': 'pg_migration', 'dataset': dataset.value},
        )
        session.add(run)
        await session.flush()

        self._log.emit_safe(
            level=LogLevel.INFO,
            message='lake_migration.run_started',
            component=_COMPONENT,
            payload=merge_emit_capability_trace_fields(
                {
                    'migration_run_id': str(run.id),
                    'dataset': dataset.value,
                    'batch_size': batch_size,
                    'lake_batch_id': str(lake_batch.id),
                    'synthetic_run_id': None,
                    'correlation_id': correlation_id,
                },
                capability_id=_CAPABILITY_ID,
                target_id='lake_migration_run',
            ),
        )
        return run

    async def _resume_run(
        self,
        session: AsyncSession,
        *,
        resume_id: uuid.UUID,
        dataset: LakeMigrationDataset,
        correlation_id: str | None,
    ) -> LakeMigrationRun:
        result = await session.execute(select(LakeMigrationRun).where(LakeMigrationRun.id == resume_id))
        run = result.scalar_one_or_none()
        if run is None:
            raise LakeMigrationNotFoundError(resume_id)
        if run.dataset != dataset:
            raise LakeMigrationDatasetError(
                f'Resume dataset mismatch: run {resume_id} has dataset '
                f'{run.dataset.value!r}, requested {dataset.value!r}.'
            )
        if run.status not in _RESUMABLE_STATUSES:
            raise LakeMigrationResumeError(
                f'Cannot resume run {resume_id} with status {run.status.value!r}. '
                'Only failed/cancelled/pending/running runs can be resumed.'
            )

        run.status = LakeMigrationStatus.running
        run.started_at = datetime.now(UTC)
        # Merge resumed_at into lake_batches metadata.
        await self._merge_lake_batch_metadata(
            session,
            lake_batch_id=run.lake_batch_id,
            extra={'resumed_at': datetime.now(UTC).isoformat()},
        )
        await session.flush()

        self._log.emit_safe(
            level=LogLevel.INFO,
            message='lake_migration.run_started',
            component=_COMPONENT,
            payload=merge_emit_capability_trace_fields(
                {
                    'migration_run_id': str(run.id),
                    'dataset': run.dataset.value,
                    'batch_size': run.batch_size,
                    'lake_batch_id': str(run.lake_batch_id),
                    'synthetic_run_id': str(run.synthetic_run_id) if run.synthetic_run_id else None,
                    'correlation_id': correlation_id,
                    'resumed': True,
                },
                capability_id=_CAPABILITY_ID,
                target_id='lake_migration_run',
            ),
        )
        return run

    async def _merge_lake_batch_metadata(
        self,
        session: AsyncSession,
        lake_batch_id: uuid.UUID,
        extra: dict[str, Any],
    ) -> None:
        from src.inventory.lake_batches.models import LakeBatch

        result = await session.execute(select(LakeBatch).where(LakeBatch.id == lake_batch_id))
        lb = result.scalar_one_or_none()
        if lb is not None:
            existing = dict(lb.metadata_json or {})
            existing.update(extra)
            lb.metadata_json = existing
        await session.flush()

    # ------------------------------------------------------------------
    # Artifact migration
    # ------------------------------------------------------------------

    async def migrate_access_artifacts(
        self,
        session: AsyncSession,
        run: LakeMigrationRun,
        *,
        lake_session: LakeSession,
        catalog: Catalog,
        correlation_id: str | None = None,
    ) -> None:
        """Stream PG access_artifacts → Iceberg raw.access_artifacts.

        4-layer idempotency:
        1. Pre-write batch check: query Iceberg for ids already present → skip.
        2. Cursor resumability: WHERE id > last_processed_id ORDER BY id.
        3. Lake-batch reuse: existing lake_batch_id is reused across batches.
        4. Snapshot id: stored as latest snapshot in lake_batches after each batch.
        """
        from pyiceberg.io.pyarrow import schema_to_pyarrow  # noqa: PLC0415

        # Mark as running.
        run.status = LakeMigrationStatus.running
        run.started_at = run.started_at or datetime.now(UTC)
        await session.flush()

        batch_size = run.batch_size
        last_id: uuid.UUID | None = run.last_processed_id

        # Load Iceberg table pa_schema once.
        iceberg_tbl = catalog.load_table(RAW_ACCESS_ARTIFACTS_TABLE)
        pa_schema = schema_to_pyarrow(iceberg_tbl.schema())

        table_path = lake_session.iceberg_table_path('raw', 'access_artifacts')

        start_ts = datetime.now(UTC)
        total_read = run.rows_read
        total_written = run.rows_written

        try:
            while True:
                # Build cursor query — raw SQL (AccessArtifact ORM removed Phase 15 Step 16).
                if last_id is not None:
                    raw = await session.execute(
                        text(
                            'SELECT id, application_id, artifact_type, external_id, payload,'
                            ' ingest_batch_id, observed_at, raw_name, effect,'
                            ' valid_from, valid_until, is_active, tombstoned_at, ingested_at'
                            ' FROM access_artifacts WHERE id > :last_id ORDER BY id LIMIT :batch_size'
                        ),
                        {'last_id': last_id, 'batch_size': batch_size},
                    )
                else:
                    raw = await session.execute(
                        text(
                            'SELECT id, application_id, artifact_type, external_id, payload,'
                            ' ingest_batch_id, observed_at, raw_name, effect,'
                            ' valid_from, valid_until, is_active, tombstoned_at, ingested_at'
                            ' FROM access_artifacts ORDER BY id LIMIT :batch_size'
                        ),
                        {'batch_size': batch_size},
                    )
                rows = raw.all()
                if not rows:
                    break

                batch_ids = [row.id for row in rows]
                batch_first_id = batch_ids[0]
                batch_last_id = batch_ids[-1]
                total_read += len(rows)

                # Pre-write check: find ids already in Iceberg.
                already_ids = self._iceberg_existing_ids(lake_session, table_path, batch_ids)
                new_rows = [r for r in rows if r.id not in already_ids]
                skipped = len(rows) - len(new_rows)

                if skipped > 0:
                    self._log.emit_safe(
                        level=LogLevel.INFO,
                        message='lake_migration.batch_skipped_idempotent',
                        component=_COMPONENT,
                        payload=merge_emit_capability_trace_fields(
                            {
                                'migration_run_id': str(run.id),
                                'dataset': run.dataset.value,
                                'batch_first_id': str(batch_first_id),
                                'batch_last_id': str(batch_last_id),
                                'skipped_row_count': skipped,
                            },
                            capability_id=_CAPABILITY_ID,
                            target_id='raw.access_artifacts',
                        ),
                    )

                snapshot_id: int = -1
                if new_rows:
                    arrow_tbl = build_artifact_arrow_table(new_rows, pa_schema=pa_schema)
                    snapshot_id = append_artifact_batch(catalog, arrow_tbl)
                    total_written += len(new_rows)

                # Checkpoint: update run in same transaction.
                run.last_processed_id = batch_last_id
                run.rows_read = total_read
                run.rows_written = total_written
                await session.flush()

                # Update lake_batch snapshot_id with latest.
                if snapshot_id != -1:
                    await self._update_lake_batch_snapshot(session, run.lake_batch_id, snapshot_id)

                self._log.emit_safe(
                    level=LogLevel.INFO,
                    message='lake_migration.batch_completed',
                    component=_COMPONENT,
                    payload=merge_emit_capability_trace_fields(
                        {
                            'migration_run_id': str(run.id),
                            'dataset': run.dataset.value,
                            'rows_processed': len(new_rows),
                            'rows_skipped_idempotent': skipped,
                            'snapshot_id': snapshot_id,
                            'last_processed_id': str(batch_last_id),
                        },
                        capability_id=_CAPABILITY_ID,
                        target_id='raw.access_artifacts',
                    ),
                )

                last_id = batch_last_id
                if len(rows) < batch_size:
                    break

            # Terminal success.
            run.status = LakeMigrationStatus.completed
            run.finished_at = datetime.now(UTC)
            run.rows_read = total_read
            run.rows_written = total_written
            await session.flush()

            duration = (datetime.now(UTC) - start_ts).total_seconds()
            self._log.emit_safe(
                level=LogLevel.INFO,
                message='lake_migration.run_completed',
                component=_COMPONENT,
                payload=merge_emit_capability_trace_fields(
                    {
                        'migration_run_id': str(run.id),
                        'dataset': run.dataset.value,
                        'rows_read': total_read,
                        'rows_written': total_written,
                        'duration_seconds': duration,
                    },
                    capability_id=_CAPABILITY_ID,
                    target_id='raw.access_artifacts',
                ),
            )

        except Exception as exc:
            run.status = LakeMigrationStatus.failed
            run.finished_at = datetime.now(UTC)
            run.error = str(exc)
            await session.flush()
            self._log.emit_safe(
                level=LogLevel.ERROR,
                message='lake_migration.run_failed',
                component=_COMPONENT,
                payload=merge_emit_capability_trace_fields(
                    {
                        'migration_run_id': str(run.id),
                        'dataset': run.dataset.value,
                        'error': str(exc),
                        'error_type': type(exc).__name__,
                        'last_processed_id': str(run.last_processed_id) if run.last_processed_id else None,
                    },
                    capability_id=_CAPABILITY_ID,
                    target_id='raw.access_artifacts',
                ),
            )
            raise

    # ------------------------------------------------------------------
    # Facts migration
    # ------------------------------------------------------------------

    async def migrate_access_facts(
        self,
        session: AsyncSession,
        run: LakeMigrationRun,
        *,
        lake_session: LakeSession,
        catalog: Catalog,
        correlation_id: str | None = None,
    ) -> None:
        """Stream PG access_facts → Iceberg normalized.access_facts.

        Additionally creates synthetic ReconciliationDeltaItems (one per fact)
        so every Iceberg row has a non-null reconciliation_delta_item_id.

        Synthetic run: shared across all batches; created once at migration start.
        """
        from pyiceberg.io.pyarrow import schema_to_pyarrow  # noqa: PLC0415

        run.status = LakeMigrationStatus.running
        run.started_at = run.started_at or datetime.now(UTC)
        await session.flush()

        batch_size = run.batch_size
        last_id: uuid.UUID | None = run.last_processed_id

        # Ensure synthetic ReconciliationRun exists (shared across all batches).
        synthetic_run = await self._ensure_synthetic_reconciliation_run(session, run)
        if run.synthetic_run_id is None:
            run.synthetic_run_id = synthetic_run.id
            await session.flush()

        iceberg_tbl = catalog.load_table(NORMALIZED_ACCESS_FACTS_TABLE)
        pa_schema = schema_to_pyarrow(iceberg_tbl.schema())

        table_path = lake_session.iceberg_table_path('normalized', 'access_facts')

        start_ts = datetime.now(UTC)
        total_read = run.rows_read
        total_written = run.rows_written

        try:
            while True:
                # Raw SQL — AccessFact ORM removed Phase 15 Step 16.
                if last_id is not None:
                    raw = await session.execute(
                        text(
                            'SELECT id, subject_id, account_id, resource_id, action_id, effect,'
                            ' valid_from, valid_until, is_active, revoked_at, observed_at, created_at'
                            ' FROM access_facts WHERE id > :last_id ORDER BY id LIMIT :batch_size'
                        ),
                        {'last_id': last_id, 'batch_size': batch_size},
                    )
                else:
                    raw = await session.execute(
                        text(
                            'SELECT id, subject_id, account_id, resource_id, action_id, effect,'
                            ' valid_from, valid_until, is_active, revoked_at, observed_at, created_at'
                            ' FROM access_facts ORDER BY id LIMIT :batch_size'
                        ),
                        {'batch_size': batch_size},
                    )
                rows = raw.all()
                if not rows:
                    break

                batch_ids = [row.id for row in rows]
                batch_first_id = batch_ids[0]
                batch_last_id = batch_ids[-1]
                total_read += len(rows)

                # Pre-write check: find fact ids already in Iceberg.
                already_ids = self._iceberg_existing_ids(lake_session, table_path, batch_ids)
                new_rows = [r for r in rows if r.id not in already_ids]
                skipped = len(rows) - len(new_rows)

                if skipped > 0:
                    self._log.emit_safe(
                        level=LogLevel.INFO,
                        message='lake_migration.batch_skipped_idempotent',
                        component=_COMPONENT,
                        payload=merge_emit_capability_trace_fields(
                            {
                                'migration_run_id': str(run.id),
                                'dataset': run.dataset.value,
                                'batch_first_id': str(batch_first_id),
                                'batch_last_id': str(batch_last_id),
                                'skipped_row_count': skipped,
                            },
                            capability_id=_CAPABILITY_ID,
                            target_id='normalized.access_facts',
                        ),
                    )

                snapshot_id: int = -1
                if new_rows:
                    # Resolve denorm data for new_rows.
                    denorm_map = await self._resolve_denorm(session, new_rows)
                    # Natural key hashes.
                    nk_hash_map = self._compute_hashes(new_rows, denorm_map)
                    # Upsert synthetic delta items (idempotent via partial unique index).
                    delta_id_map = await self._upsert_synthetic_delta_items(
                        session, synthetic_run, new_rows, nk_hash_map
                    )

                    arrow_tbl = build_fact_arrow_table(
                        new_rows,
                        pa_schema=pa_schema,
                        denorm_map=denorm_map,
                        delta_item_id_map=delta_id_map,
                        natural_key_hash_map=nk_hash_map,
                    )
                    snapshot_id = append_fact_batch(catalog, arrow_tbl)
                    total_written += len(new_rows)

                run.last_processed_id = batch_last_id
                run.rows_read = total_read
                run.rows_written = total_written
                await session.flush()

                if snapshot_id != -1:
                    await self._update_lake_batch_snapshot(session, run.lake_batch_id, snapshot_id)

                self._log.emit_safe(
                    level=LogLevel.INFO,
                    message='lake_migration.batch_completed',
                    component=_COMPONENT,
                    payload=merge_emit_capability_trace_fields(
                        {
                            'migration_run_id': str(run.id),
                            'dataset': run.dataset.value,
                            'rows_processed': len(new_rows),
                            'rows_skipped_idempotent': skipped,
                            'snapshot_id': snapshot_id,
                            'last_processed_id': str(batch_last_id),
                        },
                        capability_id=_CAPABILITY_ID,
                        target_id='normalized.access_facts',
                    ),
                )

                last_id = batch_last_id
                if len(rows) < batch_size:
                    break

            run.status = LakeMigrationStatus.completed
            run.finished_at = datetime.now(UTC)
            run.rows_read = total_read
            run.rows_written = total_written
            await session.flush()

            duration = (datetime.now(UTC) - start_ts).total_seconds()
            self._log.emit_safe(
                level=LogLevel.INFO,
                message='lake_migration.run_completed',
                component=_COMPONENT,
                payload=merge_emit_capability_trace_fields(
                    {
                        'migration_run_id': str(run.id),
                        'dataset': run.dataset.value,
                        'rows_read': total_read,
                        'rows_written': total_written,
                        'duration_seconds': duration,
                    },
                    capability_id=_CAPABILITY_ID,
                    target_id='normalized.access_facts',
                ),
            )

        except Exception as exc:
            run.status = LakeMigrationStatus.failed
            run.finished_at = datetime.now(UTC)
            run.error = str(exc)
            await session.flush()
            self._log.emit_safe(
                level=LogLevel.ERROR,
                message='lake_migration.run_failed',
                component=_COMPONENT,
                payload=merge_emit_capability_trace_fields(
                    {
                        'migration_run_id': str(run.id),
                        'dataset': run.dataset.value,
                        'error': str(exc),
                        'error_type': type(exc).__name__,
                        'last_processed_id': str(run.last_processed_id) if run.last_processed_id else None,
                    },
                    capability_id=_CAPABILITY_ID,
                    target_id='normalized.access_facts',
                ),
            )
            raise

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    async def get_run(
        self,
        session: AsyncSession,
        run_id: uuid.UUID,
    ) -> LakeMigrationRun | None:
        """Return run by id or None."""
        result = await session.execute(select(LakeMigrationRun).where(LakeMigrationRun.id == run_id))
        return result.scalar_one_or_none()

    async def list_runs(
        self,
        session: AsyncSession,
        *,
        status_filter: LakeMigrationStatus | None = None,
        dataset_filter: LakeMigrationDataset | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> tuple[list[LakeMigrationRun], str | None]:
        """Cursor-paginated list of runs (ordered created_at DESC, id DESC).

        Cursor encodes ``<created_at_iso>|<id>``.
        Returns ``(runs, next_cursor)``.
        """
        import base64  # noqa: PLC0415

        stmt = select(LakeMigrationRun)
        if status_filter is not None:
            stmt = stmt.where(LakeMigrationRun.status == status_filter)
        if dataset_filter is not None:
            stmt = stmt.where(LakeMigrationRun.dataset == dataset_filter)

        if cursor is not None:
            try:
                decoded = base64.urlsafe_b64decode(cursor.encode()).decode()
                ts_str, id_str = decoded.split('|', 1)
                cursor_ts = datetime.fromisoformat(ts_str)
                cursor_id = uuid.UUID(id_str)
                stmt = stmt.where(
                    (LakeMigrationRun.created_at < cursor_ts)
                    | ((LakeMigrationRun.created_at == cursor_ts) & (LakeMigrationRun.id < cursor_id))
                )
            except Exception:  # noqa: BLE001
                pass  # Invalid cursor — ignore, return from top.

        stmt = stmt.order_by(
            LakeMigrationRun.created_at.desc(),
            LakeMigrationRun.id.desc(),
        ).limit(limit + 1)

        result = await session.execute(stmt)
        runs = list(result.scalars().all())

        next_cursor: str | None = None
        if len(runs) > limit:
            runs = runs[:limit]
            last = runs[-1]
            raw = f'{last.created_at.isoformat()}|{last.id}'
            next_cursor = base64.urlsafe_b64encode(raw.encode()).decode()

        return runs, next_cursor

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _iceberg_existing_ids(
        self,
        lake_session: LakeSession,
        table_path: str,
        batch_ids: list[uuid.UUID],
    ) -> set[uuid.UUID]:
        """Query Iceberg for ids already present in the batch (idempotency layer 1)."""
        if not batch_ids:
            return set()
        try:
            id_strs = [str(i) for i in batch_ids]
            sql = f"SELECT DISTINCT id FROM iceberg_scan('{table_path}') WHERE id = ANY($1::varchar[])"
            lake_session.execute(sql, [id_strs])
            rows = lake_session.fetchall()
            result: set[uuid.UUID] = set()
            for row in rows:
                if row[0] is not None:
                    try:
                        result.add(uuid.UUID(str(row[0])))
                    except (ValueError, AttributeError):
                        pass
            return result
        except Exception:  # noqa: BLE001
            # Table may not exist yet (first batch) — treat as empty.
            return set()

    async def _resolve_denorm(
        self,
        session: AsyncSession,
        facts: list[Any],
    ) -> dict[uuid.UUID, tuple[uuid.UUID, str]]:
        """Resolve (application_id_denorm, subject_kind_denorm) for each fact row.

        Single PG query per batch — raw SQL, no ORM dependency.
        """
        from src.inventory.resources.models import Resource  # noqa: PLC0415
        from src.inventory.subjects.models import Subject  # noqa: PLC0415

        resource_ids = list({f.resource_id for f in facts})
        subject_ids = list({f.subject_id for f in facts})

        r_result = await session.execute(
            select(Resource.id, Resource.application_id).where(Resource.id.in_(resource_ids))
        )
        resource_app: dict[uuid.UUID, uuid.UUID] = {row[0]: row[1] for row in r_result.all()}

        s_result = await session.execute(select(Subject.id, Subject.kind).where(Subject.id.in_(subject_ids)))
        subject_kind: dict[uuid.UUID, str] = {
            row[0]: (row[1].value if hasattr(row[1], 'value') else str(row[1])) for row in s_result.all()
        }

        denorm_map: dict[uuid.UUID, tuple[uuid.UUID, str]] = {}
        for fact in facts:
            app_id = resource_app.get(fact.resource_id)
            kind = subject_kind.get(fact.subject_id, 'employee')
            if app_id is not None:
                denorm_map[fact.id] = (app_id, kind)
        return denorm_map

    def _compute_hashes(
        self,
        facts: list[Any],
        denorm_map: dict[uuid.UUID, tuple[uuid.UUID, str]],
    ) -> dict[uuid.UUID, str]:
        """Compute natural_key_hash for each fact row using the shared helper."""
        result: dict[uuid.UUID, str] = {}
        for fact in facts:
            app_id_denorm, _ = denorm_map.get(fact.id, (None, None))
            if app_id_denorm is None:
                continue
            result[fact.id] = compute_natural_key_hash(
                app_id=app_id_denorm,
                subject_id=fact.subject_id,
                account_id=fact.account_id,
                resource_id=fact.resource_id,
                action_id=fact.action_id,
                effect=str(fact.effect.value if hasattr(fact.effect, 'value') else fact.effect),
            )
        return result

    async def _ensure_synthetic_reconciliation_run(
        self,
        session: AsyncSession,
        migration_run: LakeMigrationRun,
    ) -> ReconciliationRun:
        """Get or create the shared synthetic ReconciliationRun for this migration.

        application_id=NULL (cross-app migration run), status='applied'.
        The synthetic run is identified by reason='pg_migration' in delta_items + application_id=NULL.
        """
        if migration_run.synthetic_run_id is not None:
            result = await session.execute(
                select(ReconciliationRun).where(ReconciliationRun.id == migration_run.synthetic_run_id)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                return existing

        synth_run = ReconciliationRun(
            application_id=None,  # nullable after Step 14 migration
            status=ReconciliationRunStatus.applied,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC),
        )
        session.add(synth_run)
        await session.flush()
        return synth_run

    async def _upsert_synthetic_delta_items(
        self,
        session: AsyncSession,
        synthetic_run: ReconciliationRun,
        facts: list[Any],
        nk_hash_map: dict[uuid.UUID, str],
    ) -> dict[uuid.UUID, uuid.UUID]:
        """Bulk-upsert synthetic ReconciliationDeltaItem rows for migrated facts.

        Idempotency: partial unique index on (reason, existing_fact_id) WHERE reason='pg_migration'.
        Returns: dict fact.id → delta_item.id
        """
        delta_id_map: dict[uuid.UUID, uuid.UUID] = {}

        if not facts:
            return delta_id_map

        # Check which delta items already exist for this batch.
        fact_ids = [f.id for f in facts]
        existing_result = await session.execute(
            select(ReconciliationDeltaItem.id, ReconciliationDeltaItem.existing_fact_id).where(
                ReconciliationDeltaItem.existing_fact_id.in_(fact_ids),
                ReconciliationDeltaItem.reason == 'pg_migration',
            )
        )
        existing_map: dict[uuid.UUID, uuid.UUID] = {row[1]: row[0] for row in existing_result.all()}

        # Build items to insert.
        now = datetime.now(UTC)
        to_insert: list[ReconciliationDeltaItem] = []
        for fact in facts:
            if fact.id in existing_map:
                delta_id_map[fact.id] = existing_map[fact.id]
                continue

            nk_hash = nk_hash_map.get(fact.id, '')
            after_json = self._build_after_json(fact)
            item = ReconciliationDeltaItem(
                reconciliation_run_id=synthetic_run.id,
                operation=ReconciliationDeltaOperation.create,
                natural_key_hash=nk_hash,
                subject_id=fact.subject_id,
                account_id=fact.account_id,
                resource_id=fact.resource_id,
                action_id=fact.action_id,
                effect=str(fact.effect.value if hasattr(fact.effect, 'value') else fact.effect),
                existing_fact_id=fact.id,
                source_artifact_id=None,
                before_json=None,
                after_json=after_json,
                status=ReconciliationDeltaItemStatus.applied,
                reason='pg_migration',
                applied_at=fact.created_at,
                created_at=now,
            )
            to_insert.append(item)

        if to_insert:
            session.add_all(to_insert)
            await session.flush()
            for item in to_insert:
                if item.existing_fact_id is not None:
                    delta_id_map[item.existing_fact_id] = item.id

        return delta_id_map

    def _build_after_json(self, fact: Any) -> dict[str, Any]:
        """Build the after_json snapshot for a synthetic delta item (§4.2 shape)."""
        effect_val = fact.effect.value if hasattr(fact.effect, 'value') else str(fact.effect)
        return {
            'origin': 'pg_migration',
            'fact_id': str(fact.id),
            'subject_id': str(fact.subject_id),
            'account_id': str(fact.account_id) if fact.account_id else None,
            'resource_id': str(fact.resource_id),
            'action_id': fact.action_id,
            'effect': effect_val,
            'valid_from': fact.valid_from.isoformat() if fact.valid_from else None,
            'valid_until': fact.valid_until.isoformat() if fact.valid_until else None,
            'is_active': fact.is_active,
            'observed_at': fact.observed_at.isoformat() if fact.observed_at else None,
            'created_at': fact.created_at.isoformat() if fact.created_at else None,
            'revoked_at': fact.revoked_at.isoformat() if fact.revoked_at is not None else None,
            'application_id_denorm': None,  # filled by caller if available
            'subject_kind_denorm': None,  # filled by caller if available
        }

    async def _update_lake_batch_snapshot(
        self,
        session: AsyncSession,
        lake_batch_id: uuid.UUID,
        snapshot_id: int,
    ) -> None:
        from src.inventory.lake_batches.models import LakeBatch  # noqa: PLC0415

        result = await session.execute(select(LakeBatch).where(LakeBatch.id == lake_batch_id))
        lb = result.scalar_one_or_none()
        if lb is not None:
            lb.snapshot_id = snapshot_id
        await session.flush()

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact service — domain-facing façade for normalized.access_facts.

Phase 17 Step 19: Physical DuckDB read paths extracted to
``src/platform/lake/access_facts_reader.py``. This module is now a thin
façade: input validation, observability ``emit_safe`` lines, multi-store
``get_artifact_ref`` orchestration (lake → PG → lake), and the
``_row_to_view(AccessFactRow) → AccessFactView`` inventory-boundary mapping.

Phase 15 Step 16: PG write methods removed (create_fact, revoke_fact,
refresh_fact_fields were dead since Step 12). ORM imports removed.
Service reads from normalized.access_facts via DuckDB iceberg_scan.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.access_facts.schemas import AccessFactArtifactRefRead, AccessFactEffect, AccessFactView
from src.platform.lake.access_facts_reader import (
    AccessFactRow,
    run_get_artifact_from_iceberg,
    run_get_by_natural_key,
    run_get_delta_item_id,
    run_get_fact,
    run_list_facts,
)
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import (
    LogService,
    NoOpLogService,
    merge_emit_log_participant_fields,
    noop_log_service,
)

_COMPONENT = 'inventory.access_facts'

# ---------------------------------------------------------------------------
# Domain errors
# ---------------------------------------------------------------------------


class AccessFactNotFoundError(Exception):
    """Raised when an access fact is not found."""

    def __init__(self, fact_id: uuid.UUID) -> None:
        self.fact_id = fact_id
        super().__init__(f'Access fact not found: {fact_id}')


class AccessFactArtifactRefNotFoundError(Exception):
    """Raised when the artifact reference chain is broken for a given fact_id.

    Any of the three lookups (fact, delta_item, artifact) returning empty
    raises this error with unified semantics → 404 at the route layer.
    """

    def __init__(self, fact_id: uuid.UUID) -> None:
        self.fact_id = fact_id
        super().__init__(f'Access fact artifact reference not found: {fact_id}')


# ---------------------------------------------------------------------------
# Inventory-boundary DTO mapper
# ---------------------------------------------------------------------------


def _row_to_view(row: AccessFactRow) -> AccessFactView:
    """Convert a lake-level :class:`~src.platform.lake.access_facts_reader.AccessFactRow`
    to an inventory-level :class:`AccessFactView`.

    UUID coercion and action_id casting remain here — this is the inventory
    boundary contract, not a lake-layer concern.
    """
    d: dict[str, Any] = {
        'id': row.id,
        'subject_id': row.subject_id,
        'account_id': row.account_id,
        'resource_id': row.resource_id,
        'action_id': row.action_id,
        'action_slug': row.action_slug,
        'effect': row.effect,
        'valid_from': row.valid_from,
        'valid_until': row.valid_until,
        'is_active': row.is_active,
        'revoked_at': row.revoked_at,
        'observed_at': row.observed_at,
        'created_at': row.created_at,
    }
    # Cast action_id to int (stored as string in some lake schemas)
    if d.get('action_id') is not None:
        d['action_id'] = int(d['action_id'])
    # Normalise UUIDs
    for field in ('id', 'subject_id', 'resource_id'):
        if d.get(field) is not None and not isinstance(d[field], uuid.UUID):
            d[field] = uuid.UUID(str(d[field]))
    if d.get('account_id') is not None and not isinstance(d['account_id'], uuid.UUID):
        d['account_id'] = uuid.UUID(str(d['account_id']))
    return AccessFactView.model_validate(d, strict=False)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AccessFactService:
    """Domain-facing façade for normalized.access_facts.

    Phase 17 Step 19: DuckDB read paths delegated to
    ``src/platform/lake/access_facts_reader.py``.

    Phase 15 Step 16: write methods removed (create_fact, revoke_fact,
    refresh_fact_fields). event_service param removed. Reads go via DuckDB.

    NOTE: event_service and session-based constructor params are accepted for
    backward-compat with callers (effective_access tests)
    that have not yet been migrated. They are ignored.
    """

    def __init__(
        self,
        event_service: Any = None,  # noqa: ARG002 — backward compat, ignored
        *,
        log_service: LogService | None = None,
    ) -> None:
        self._log: LogService | NoOpLogService = log_service if log_service is not None else noop_log_service

    def _get_warehouse_uri(self, lake_session: Any) -> str:
        """Extract warehouse URI from lake_session if available."""
        if hasattr(lake_session, 'warehouse_uri'):
            uri = lake_session.warehouse_uri
            return str(uri) if uri is not None else ''
        # Fallback: derive from iceberg_table_path convention
        if hasattr(lake_session, 'iceberg_table_path'):
            path: str = lake_session.iceberg_table_path('normalized', 'access_facts')
            # strip namespace+table suffix to get warehouse root
            parts = path.split('/')
            if len(parts) >= 3:
                return '/'.join(parts[:-2])
        return ''

    async def create_fact(self, *args: Any, **kwargs: Any) -> Any:
        """Removed in Phase 15 Step 16. Fact mutation flows through SyncApplyService.

        This stub raises NotImplementedError at runtime.
        """
        raise NotImplementedError(
            'AccessFactService.create_fact was removed in Phase 15 Step 16. '
            'Fact mutation must flow through SyncApplyService Iceberg writer.'
        )

    async def revoke_fact(self, *args: Any, **kwargs: Any) -> Any:
        """Removed in Phase 15 Step 16. See create_fact docstring."""
        raise NotImplementedError('AccessFactService.revoke_fact was removed in Phase 15 Step 16.')

    async def refresh_fact_fields(self, *args: Any, **kwargs: Any) -> Any:
        """Removed in Phase 15 Step 16. See create_fact docstring."""
        raise NotImplementedError('AccessFactService.refresh_fact_fields was removed in Phase 15 Step 16.')

    async def get_fact(
        self,
        lake_session: Any,
        fact_id: uuid.UUID,
    ) -> AccessFactView | None:
        """Get access fact by id via DuckDB iceberg_scan. Returns DTO or None."""
        warehouse_uri = self._get_warehouse_uri(lake_session)

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_facts.get_started',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {'fact_id': str(fact_id)},
                actor_component=_COMPONENT,
                target_id=str(fact_id),
            ),
        )

        row = await asyncio.to_thread(
            run_get_fact,
            lake_session,
            warehouse_uri=warehouse_uri,
            fact_id=fact_id,
        )

        view = _row_to_view(row) if row is not None else None

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_facts.get_completed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {'fact_id': str(fact_id), 'found': view is not None},
                actor_component=_COMPONENT,
                target_id=str(fact_id),
            ),
        )
        return view

    async def list_facts(
        self,
        lake_session: Any,
        *,
        subject_id: uuid.UUID | None = None,
        resource_id: uuid.UUID | None = None,
        account_id: uuid.UUID | None = None,
        action_slug: str | None = None,
        effect: AccessFactEffect | None = None,
        is_active: bool | None = None,
        valid_at: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AccessFactView]:
        """List access facts with optional filters via DuckDB iceberg_scan."""
        warehouse_uri = self._get_warehouse_uri(lake_session)

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_facts.list_started',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'subject_id': str(subject_id) if subject_id else None,
                    'limit': limit,
                    'offset': offset,
                },
                actor_component=_COMPONENT,
                target_id='list',
            ),
        )

        rows = await asyncio.to_thread(
            run_list_facts,
            lake_session,
            warehouse_uri=warehouse_uri,
            subject_id=subject_id,
            resource_id=resource_id,
            account_id=account_id,
            action_slug=action_slug,
            effect_value=effect.value if effect is not None else None,
            is_active=is_active,
            valid_at=valid_at,
            limit=limit,
            offset=offset,
        )

        views = [_row_to_view(r) for r in rows]

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_facts.list_completed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {'row_count': len(views)},
                actor_component=_COMPONENT,
                target_id='list',
            ),
        )
        return views

    async def get_artifact_ref(
        self,
        lake_session: Any,
        pg_session: AsyncSession,
        fact_id: uuid.UUID,
    ) -> AccessFactArtifactRefRead:
        """Resolve the drill-down chain: access_fact → delta_item → access_artifact.

        Steps:
        1. DuckDB iceberg_scan normalized.access_facts WHERE id=fact_id → reconciliation_delta_item_id.
        2. PG SELECT source_artifact_id FROM reconciliation_delta_items WHERE id=delta_item_id.
        3. DuckDB iceberg_scan raw.access_artifacts WHERE id=source_artifact_id → (application_id, external_id).

        Raises AccessFactArtifactRefNotFoundError on any broken link (unified 404 semantics).
        """
        warehouse_uri = self._get_warehouse_uri(lake_session)

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_facts.artifact_ref_resolve_started',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {'fact_id': str(fact_id)},
                actor_component=_COMPONENT,
                target_id=str(fact_id),
            ),
        )

        # Step 1: fact → reconciliation_delta_item_id
        delta_item_id = await asyncio.to_thread(
            run_get_delta_item_id,
            lake_session,
            warehouse_uri=warehouse_uri,
            fact_id=fact_id,
        )

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_facts.artifact_ref_step1_done',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {'fact_id': str(fact_id), 'delta_item_id': str(delta_item_id) if delta_item_id else None},
                actor_component=_COMPONENT,
                target_id=str(fact_id),
            ),
        )

        if delta_item_id is None:
            raise AccessFactArtifactRefNotFoundError(fact_id)

        # Step 2: delta_item_id → source_artifact_id via PG
        result = await pg_session.execute(
            sa.text('SELECT source_artifact_id FROM reconciliation_delta_items WHERE id = :id LIMIT 1'),
            {'id': delta_item_id},
        )
        row = result.fetchone()

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_facts.artifact_ref_step2_done',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'delta_item_id': str(delta_item_id),
                    'source_artifact_id': str(row[0]) if row and row[0] is not None else None,
                },
                actor_component=_COMPONENT,
                target_id=str(fact_id),
            ),
        )

        if row is None or row[0] is None:
            raise AccessFactArtifactRefNotFoundError(fact_id)

        source_artifact_id: uuid.UUID = row[0]

        # Step 3: source_artifact_id → (application_id, external_id) via Iceberg
        artifact_fields = await asyncio.to_thread(
            run_get_artifact_from_iceberg,
            lake_session,
            warehouse_uri=warehouse_uri,
            artifact_id=source_artifact_id,
        )

        if artifact_fields is None:
            raise AccessFactArtifactRefNotFoundError(fact_id)

        application_id, external_id = artifact_fields

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.INFO,
            message='inventory.access_facts.artifact_ref_resolved',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'fact_id': str(fact_id),
                    'artifact_id': str(source_artifact_id),
                    'application_id': str(application_id),
                },
                actor_component=_COMPONENT,
                target_id=str(fact_id),
            ),
        )

        return AccessFactArtifactRefRead(
            artifact_id=source_artifact_id,
            application_id=application_id,
            external_id=external_id,
        )

    async def get_fact_by_natural_key(
        self,
        lake_session: Any,
        *,
        subject_id: uuid.UUID,
        account_id: uuid.UUID | None,
        resource_id: uuid.UUID,
        action_slug: str,
    ) -> AccessFactView | None:
        """Look up an ACTIVE access fact by natural key via DuckDB iceberg_scan."""
        warehouse_uri = self._get_warehouse_uri(lake_session)

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_facts.get_by_natural_key_started',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'subject_id': str(subject_id),
                    'resource_id': str(resource_id),
                    'action_slug': action_slug,
                },
                actor_component=_COMPONENT,
                target_id='natural_key',
            ),
        )

        row = await asyncio.to_thread(
            run_get_by_natural_key,
            lake_session,
            warehouse_uri=warehouse_uri,
            subject_id=subject_id,
            account_id=account_id,
            resource_id=resource_id,
            action_slug=action_slug,
        )

        view = _row_to_view(row) if row is not None else None

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.DEBUG,
            message='inventory.access_facts.get_by_natural_key_completed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {'found': view is not None},
                actor_component=_COMPONENT,
                target_id='natural_key',
            ),
        )
        return view

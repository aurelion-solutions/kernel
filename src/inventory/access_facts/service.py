# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""AccessFact service — lake-read-only facade.

Phase 15 Step 16: PG write methods removed (create_fact, revoke_fact,
refresh_fact_fields were dead since Step 12). ORM imports removed.
Service reads from normalized.access_facts via DuckDB iceberg_scan.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
import uuid

from src.inventory.access_facts.schemas import AccessFactEffect, AccessFactView
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


# ---------------------------------------------------------------------------
# Backward-compat stubs (Phase 15 Step 16)
# These error classes were removed when write methods were deleted.
# They are retained here as stubs so that external callers (normalization/acl)
# that import them by name don't break before those slices are migrated.
# TODO: remove after normalization/acl is migrated away from AccessFact PG writes.
# ---------------------------------------------------------------------------


class DuplicateActiveAccessFactError(Exception):
    """Stub — raised in legacy PG write path (removed in Step 16). Do not use."""

    def __init__(self, detail: str = '') -> None:
        self.detail = detail
        super().__init__(detail)


class AccessFactForeignKeyError(Exception):
    """Stub — raised in legacy PG write path (removed in Step 16). Do not use."""

    def __init__(self, detail: str = '') -> None:
        self.detail = detail
        super().__init__(detail)


class AccessFactActionSlugUnknownError(Exception):
    """Stub — raised in legacy PG write path (removed in Step 16). Do not use."""

    def __init__(self, slug: str = '') -> None:
        self.slug = slug
        super().__init__(f'Unknown action slug: {slug!r}')


class AccessFactApplicationScopeMismatchError(Exception):
    """Stub — raised in legacy PG write path (removed in Step 16). Do not use."""

    def __init__(self, account_id: uuid.UUID | None = None, resource_id: uuid.UUID | None = None) -> None:
        super().__init__(f'Scope mismatch: account={account_id}, resource={resource_id}')


class AccessFactNotRevokedError(Exception):
    """Stub — raised in legacy PG write path (removed in Step 16). Do not use."""

    def __init__(self, fact_id: uuid.UUID | None = None) -> None:
        self.fact_id = fact_id
        super().__init__(f'Access fact {fact_id} is already revoked')


class AccessFactNotActiveError(Exception):
    """Stub — raised in legacy PG write path (removed in Step 16). Do not use."""

    def __init__(self, fact_id: uuid.UUID | None = None) -> None:
        self.fact_id = fact_id
        super().__init__(f'Access fact {fact_id} is not active')


# ---------------------------------------------------------------------------
# SQL helpers (blocking — called via asyncio.to_thread)
# ---------------------------------------------------------------------------

_FACT_COLUMNS = (
    'id',
    'subject_id',
    'account_id',
    'resource_id',
    'action_id',
    'action_slug',
    'effect',
    'valid_from',
    'valid_until',
    'is_active',
    'revoked_at',
    'observed_at',
    'created_at',
)


def _row_to_view(row: tuple[Any, ...]) -> AccessFactView:
    """Convert a DuckDB result row to AccessFactView."""
    d = dict(zip(_FACT_COLUMNS, row, strict=True))
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


def _run_get_fact(
    lake_session: Any,
    *,
    warehouse_uri: str,
    fact_id: uuid.UUID,
) -> AccessFactView | None:
    """DuckDB iceberg_scan for a single fact by id. Blocking."""
    sql = f"""
        SELECT
            f.id, f.subject_id, f.account_id, f.resource_id,
            f.action_id, r.slug AS action_slug, f.effect,
            f.valid_from, f.valid_until, f.is_active, f.revoked_at,
            f.observed_at, f.created_at
        FROM iceberg_scan('{warehouse_uri}/normalized/access_facts', skip_schema_inference=true) f
        LEFT JOIN ref_actions_local r ON r.id = CAST(f.action_id AS BIGINT)
        WHERE f.id = ?::uuid AND f.id IS NOT NULL
        LIMIT 1
    """
    lake_session.execute(sql, [str(fact_id)])
    rows = lake_session.fetchmany(1)
    if not rows:
        return None
    return _row_to_view(rows[0])


def _run_list_facts(
    lake_session: Any,
    *,
    warehouse_uri: str,
    subject_id: uuid.UUID | None,
    resource_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
    action_slug: str | None,
    effect: AccessFactEffect | None,
    is_active: bool | None,
    valid_at: datetime | None,
    limit: int,
    offset: int,
) -> list[AccessFactView]:
    """DuckDB iceberg_scan for access facts with filters. Blocking."""
    predicates: list[str] = ['f.id IS NOT NULL']
    params: list[Any] = []

    if subject_id is not None:
        predicates.append('f.subject_id = ?::uuid')
        params.append(str(subject_id))
    if resource_id is not None:
        predicates.append('f.resource_id = ?::uuid')
        params.append(str(resource_id))
    if account_id is not None:
        predicates.append('f.account_id = ?::uuid')
        params.append(str(account_id))
    if action_slug is not None:
        predicates.append('r.slug = ?')
        params.append(action_slug)
    if effect is not None:
        predicates.append('f.effect = ?')
        params.append(effect.value)
    if is_active is not None:
        predicates.append('f.is_active = ?')
        params.append(is_active)
    if valid_at is not None:
        predicates.append('f.valid_from <= ?')
        params.append(valid_at)
        predicates.append('(f.valid_until IS NULL OR f.valid_until >= ?)')
        params.append(valid_at)

    where_clause = 'WHERE ' + ' AND '.join(predicates)

    sql = f"""
        SELECT
            f.id, f.subject_id, f.account_id, f.resource_id,
            f.action_id, r.slug AS action_slug, f.effect,
            f.valid_from, f.valid_until, f.is_active, f.revoked_at,
            f.observed_at, f.created_at
        FROM iceberg_scan('{warehouse_uri}/normalized/access_facts', skip_schema_inference=true) f
        LEFT JOIN ref_actions_local r ON r.id = CAST(f.action_id AS BIGINT)
        {where_clause}
        ORDER BY f.id
        LIMIT ?
        OFFSET ?
    """
    params.append(min(limit, 200))
    params.append(offset)

    lake_session.execute(sql, params)
    views: list[AccessFactView] = []
    while True:
        batch = lake_session.fetchmany(500)
        if not batch:
            break
        for row in batch:
            views.append(_row_to_view(row))
    return views


def _run_get_by_natural_key(
    lake_session: Any,
    *,
    warehouse_uri: str,
    subject_id: uuid.UUID,
    account_id: uuid.UUID | None,
    resource_id: uuid.UUID,
    action_slug: str,
) -> AccessFactView | None:
    """DuckDB iceberg_scan for active fact by natural key. Blocking."""
    predicates = [
        'f.subject_id = ?::uuid',
        'f.resource_id = ?::uuid',
        'r.slug = ?',
        'f.is_active = true',
        'f.id IS NOT NULL',
    ]
    params: list[Any] = [str(subject_id), str(resource_id), action_slug]

    if account_id is None:
        predicates.append('f.account_id IS NULL')
    else:
        predicates.append('f.account_id = ?::uuid')
        params.append(str(account_id))

    where_clause = 'WHERE ' + ' AND '.join(predicates)

    sql = f"""
        SELECT
            f.id, f.subject_id, f.account_id, f.resource_id,
            f.action_id, r.slug AS action_slug, f.effect,
            f.valid_from, f.valid_until, f.is_active, f.revoked_at,
            f.observed_at, f.created_at
        FROM iceberg_scan('{warehouse_uri}/normalized/access_facts', skip_schema_inference=true) f
        LEFT JOIN ref_actions_local r ON r.id = CAST(f.action_id AS BIGINT)
        {where_clause}
        LIMIT 1
    """

    lake_session.execute(sql, params)
    rows = lake_session.fetchmany(1)
    if not rows:
        return None
    return _row_to_view(rows[0])


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class AccessFactService:
    """Lake-read-only facade for normalized.access_facts.

    Phase 15 Step 16: write methods removed (create_fact, revoke_fact,
    refresh_fact_fields). event_service param removed. Reads go via DuckDB.

    NOTE: event_service and session-based constructor params are accepted for
    backward-compat with callers (normalization/acl, effective_access tests)
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

        This stub exists for backward compatibility with normalization/acl callers
        that have not yet been migrated. Raises NotImplementedError at runtime.
        """
        raise NotImplementedError(
            'AccessFactService.create_fact was removed in Phase 15 Step 16. '
            'Fact mutation must flow through SyncApplyService + lake_writer.'
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
        import asyncio

        warehouse_uri = self._get_warehouse_uri(lake_session)

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

        view = await asyncio.to_thread(
            _run_get_fact,
            lake_session,
            warehouse_uri=warehouse_uri,
            fact_id=fact_id,
        )

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
        import asyncio

        warehouse_uri = self._get_warehouse_uri(lake_session)

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

        views = await asyncio.to_thread(
            _run_list_facts,
            lake_session,
            warehouse_uri=warehouse_uri,
            subject_id=subject_id,
            resource_id=resource_id,
            account_id=account_id,
            action_slug=action_slug,
            effect=effect,
            is_active=is_active,
            valid_at=valid_at,
            limit=limit,
            offset=offset,
        )

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
        import asyncio

        warehouse_uri = self._get_warehouse_uri(lake_session)

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

        view = await asyncio.to_thread(
            _run_get_by_natural_key,
            lake_session,
            warehouse_uri=warehouse_uri,
            subject_id=subject_id,
            account_id=account_id,
            resource_id=resource_id,
            action_slug=action_slug,
        )

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

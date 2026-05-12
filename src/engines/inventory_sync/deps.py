# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""SyncApplyService FastAPI dependency."""

from __future__ import annotations

from collections.abc import Callable
import os
from typing import TYPE_CHECKING, Any

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession
from src.core.db.deps import get_db
from src.engines.inventory_sync.service import SyncApplyService
from src.platform.events.factory import event_sink_factory
from src.platform.events.service import EventService
from src.platform.lake.deps import get_lake_catalog, get_lake_session
from src.platform.logs.deps import get_log_service

if TYPE_CHECKING:
    from pyiceberg.catalog import Catalog
    from src.engines.inventory_reconcile.models import ReconciliationDeltaItem
    from src.platform.lake.duckdb_session import LakeSession
    from src.platform.logs.service import LogService, NoOpLogService


def _build_event_service() -> EventService:
    provider = os.environ.get('AURELION_EVENTS_PROVIDER', 'noop')
    sink = event_sink_factory.get(provider)
    return EventService(sink=sink)


# Type for the denorm resolver with an attached async preload coroutine factory.
_DenormResolverWithPreload = Callable[['ReconciliationDeltaItem'], tuple[str, str]]


def _make_simple_denorm_resolver() -> _DenormResolverWithPreload:
    """Return a denorm resolver that reads application_id and subject_kind from
    the ReconciliationRun / Subject tables.

    Because ``DenormResolver`` is synchronous (called from inside
    ``write_run_batch`` which is sync), we use a closure with mutable caches.
    The ``preload`` coroutine factory is attached as an attribute; call it BEFORE
    passing the resolver to ``write_run_batch``.
    """
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    _run_app_cache: dict[str, str] = {}
    _subject_kind_cache: dict[str, str] = {}

    def _resolve(item: Any) -> tuple[str, str]:
        from src.engines.inventory_reconcile.models import ReconciliationDeltaItem as _Item

        assert isinstance(item, _Item)
        run_id_str = str(item.reconciliation_run_id)
        subject_id_str = str(item.subject_id)
        app_id = _run_app_cache.get(run_id_str, '')
        subject_kind = _subject_kind_cache.get(subject_id_str, 'employee')
        return app_id, subject_kind

    async def _preload(session: _AsyncSession, items: list[Any]) -> None:
        from sqlalchemy import select as _select
        from src.engines.inventory_reconcile.models import ReconciliationDeltaItem as _Item
        from src.engines.inventory_reconcile.models import ReconciliationRun
        from src.inventory.subjects.models import Subject

        typed_items = [i for i in items if isinstance(i, _Item)]
        run_ids = list({i.reconciliation_run_id for i in typed_items})
        subject_ids = list({i.subject_id for i in typed_items})

        if run_ids:
            r = await session.execute(
                _select(ReconciliationRun.id, ReconciliationRun.application_id).where(ReconciliationRun.id.in_(run_ids))
            )
            for row in r.all():
                _run_app_cache[str(row[0])] = str(row[1])

        if subject_ids:
            r = await session.execute(_select(Subject.id, Subject.kind).where(Subject.id.in_(subject_ids)))
            for row in r.all():
                kind_val = row[1]
                _subject_kind_cache[str(row[0])] = kind_val.value if hasattr(kind_val, 'value') else str(kind_val)

    _resolve.preload = _preload  # type: ignore[attr-defined]
    return _resolve


_DependsDB = Depends(get_db)
_DependsLakeSession = Depends(get_lake_session)
_DependsLakeCatalog = Depends(get_lake_catalog)


async def get_sync_apply_service(
    request: Request,
    session: AsyncSession = _DependsDB,
    lake_session: LakeSession = _DependsLakeSession,
    catalog: Catalog = _DependsLakeCatalog,
) -> SyncApplyService:
    """Build SyncApplyService with all dependencies wired via FastAPI DI."""
    log_service: LogService | NoOpLogService = get_log_service(request)
    event_service = _build_event_service()
    denorm_resolver = _make_simple_denorm_resolver()
    return SyncApplyService(
        session=session,
        lake_session=lake_session,
        catalog=catalog,
        denorm_resolver=denorm_resolver,
        events=event_service,
        logs=log_service,
    )

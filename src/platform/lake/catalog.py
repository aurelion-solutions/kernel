# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""PyIceberg SQL catalog bootstrap."""

import threading

from pyiceberg.catalog import Catalog, load_catalog
from src.platform.lake.config import LakeSettings
from src.platform.lake.exceptions import LakeCatalogError
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import LogService, merge_emit_log_participant_fields

_catalog: Catalog | None = None
_catalog_lock: threading.Lock = threading.Lock()

_COMPONENT = 'platform.lake'


def get_catalog(settings: LakeSettings, log_service: LogService) -> Catalog:
    """Return the cached PyIceberg SQL catalog, initializing it on first call.

    Called once from the FastAPI lifespan (before any request handler runs).
    The module-level lock provides defensive safety for any edge case where the
    function is called concurrently.

    On success, emits ``platform.lake.catalog_initialized`` INFO log.
    On failure, emits ``platform.lake.catalog_init_failed`` ERROR log and raises
    :class:`~src.platform.lake.exceptions.LakeCatalogError`.
    """
    global _catalog

    if _catalog is not None:
        return _catalog

    with _catalog_lock:
        # Double-checked locking: re-read module state inside the lock.
        # mypy does not track cross-thread state changes, so we read from globals() explicitly.
        cached: Catalog | None = globals().get('_catalog')
        if cached is not None:
            return cached

        try:
            catalog = load_catalog(
                settings.catalog_name,
                **{
                    'type': 'sql',
                    'uri': settings.catalog_url,
                    'warehouse': settings.warehouse_uri,
                },
            )
        except Exception as exc:  # noqa: BLE001 # allowed-broad: provider boundary
            log_service.emit_safe(
                level=LogLevel.ERROR,
                message='platform.lake.catalog_init_failed',
                component=_COMPONENT,
                payload=merge_emit_log_participant_fields(
                    {
                        'catalog_name': settings.catalog_name,
                        'warehouse_uri': settings.warehouse_uri,
                        'error': str(exc),
                    },
                    actor_component=_COMPONENT,
                    target_id='catalog',
                ),
            )
            raise LakeCatalogError(f'Failed to initialize Iceberg catalog: {exc}') from exc

        _bootstrap_namespaces(catalog, log_service)

        log_service.emit_safe(
            level=LogLevel.INFO,
            message='platform.lake.catalog_initialized',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'catalog_name': settings.catalog_name,
                    'warehouse_uri': settings.warehouse_uri,
                    'storage_provider': settings.storage_provider,
                },
                actor_component=_COMPONENT,
                target_id='catalog',
            ),
        )

        _catalog = catalog
        return _catalog


def _bootstrap_namespaces(catalog: Catalog, log_service: LogService) -> None:
    """Create raw and normalized namespaces if they do not exist."""
    from src.platform.lake.schemas import NORMALIZED_NAMESPACE, RAW_NAMESPACE

    for ns in (RAW_NAMESPACE, NORMALIZED_NAMESPACE):
        existing = [tuple(n) for n in catalog.list_namespaces()]
        if ns not in existing:
            try:
                catalog.create_namespace(ns)
            except Exception:  # noqa: BLE001 # allowed-broad: best-effort cleanup
                # Namespace may have been created concurrently; idempotent.
                pass


def reset_catalog_cache_for_tests() -> None:
    """Clear the module-level catalog cache.

    **Test-only escape hatch.** Not exported via ``__init__``.
    """
    global _catalog
    with _catalog_lock:
        _catalog = None

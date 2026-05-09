# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Smoke import gate: key modules import cleanly with no ORM residuals.

Verifies that all key modules import without error and that the handler
contract uses AccessArtifactView (not ORM).
"""

from __future__ import annotations


def test_router_v0_imports_cleanly() -> None:
    """src.routers.v0 imports without ImportError."""
    import src.routers.v0  # noqa: F401


def test_platform_api_main_imports_cleanly() -> None:
    """src.runtimes.platform_api.main imports without ImportError."""
    import src.runtimes.platform_api.main  # noqa: F401


def test_reconciliation_pipeline_imports_cleanly() -> None:
    """src.engines.reconciliation.pipeline imports without ImportError."""
    import src.engines.reconciliation.pipeline  # noqa: F401


def test_sync_apply_service_imports_cleanly() -> None:
    """src.engines.sync_apply.service imports without ImportError."""
    import src.engines.sync_apply.service  # noqa: F401


def test_access_artifact_view_exported_from_schemas() -> None:
    """AccessArtifactView is exported from access_artifacts.schemas."""
    from src.inventory.access_artifacts.schemas import AccessArtifactView

    assert AccessArtifactView is not None


def test_access_fact_view_exported_from_schemas() -> None:
    """AccessFactView and AccessFactEffect are exported from access_facts.schemas."""
    from src.inventory.access_facts.schemas import AccessFactEffect, AccessFactView

    assert AccessFactView is not None
    assert AccessFactEffect is not None
    assert AccessFactEffect.allow == 'allow'
    assert AccessFactEffect.deny == 'deny'


def test_handler_contract_uses_access_artifact_view() -> None:
    """Handler Protocol is typed with AccessArtifactView, not AccessArtifact ORM."""
    import typing

    from src.engines.reconciliation.handlers.role import RoleHandler
    from src.inventory.access_artifacts.schemas import AccessArtifactView

    role_handler = RoleHandler()
    # Use get_type_hints() to resolve PEP-563 stringified annotations
    # (from __future__ import annotations) back to the actual class objects.
    handle_hints = typing.get_type_hints(role_handler.handle)
    artifact_type = handle_hints.get('artifact')
    assert artifact_type is AccessArtifactView, (
        f'RoleHandler.handle artifact param should be AccessArtifactView, got {artifact_type}'
    )


def test_access_fact_service_write_methods_raise_not_implemented() -> None:
    """AccessFactService write methods are stubs that raise NotImplementedError (Iceberg lake migration).

    They MUST raise NotImplementedError at runtime to prevent silent data corruption.
    """
    import asyncio

    from src.inventory.access_facts.service import AccessFactService

    svc = AccessFactService()

    async def _check() -> None:
        try:
            await svc.create_fact()  # type: ignore[call-arg]
        except NotImplementedError:
            pass
        else:
            raise AssertionError('create_fact should raise NotImplementedError')

    asyncio.run(_check())


def test_access_artifact_service_pg_methods_raise_not_implemented() -> None:
    """AccessArtifactService PG-path methods are stubs that raise NotImplementedError.

    They exist for backward compat with not-yet-migrated callers but raise
    NotImplementedError to prevent silent data corruption.
    """
    import asyncio

    from src.inventory.access_artifacts.service import AccessArtifactService

    svc = AccessArtifactService()

    async def _check() -> None:
        try:
            await svc.upsert_artifact()  # type: ignore[call-arg]
        except NotImplementedError:
            pass
        else:
            raise AssertionError('upsert_artifact should raise NotImplementedError')

    asyncio.run(_check())


def test_lake_settings_default_is_iceberg() -> None:
    """LakeSettings.artifacts_write_backend default is 'iceberg'."""
    import os

    # Unset env var to test the default
    original = os.environ.pop('LAKE_ARTIFACTS_WRITE_BACKEND', None)
    try:
        from src.platform.lake.config import LakeSettings

        # Force re-instantiation to pick up default
        settings = LakeSettings(
            catalog_url='postgresql+psycopg2://x@x/x',
            warehouse_uri='file:///tmp',
        )
        assert settings.artifacts_write_backend == 'iceberg', (
            f'Expected iceberg, got {settings.artifacts_write_backend}'
        )
    finally:
        if original is not None:
            os.environ['LAKE_ARTIFACTS_WRITE_BACKEND'] = original


def test_access_fact_effect_importable_from_schemas() -> None:
    """AccessFactEffect can be imported from schemas."""
    from src.inventory.access_facts.schemas import AccessFactEffect

    assert AccessFactEffect is not None
    assert AccessFactEffect.allow == 'allow'
    assert AccessFactEffect.deny == 'deny'

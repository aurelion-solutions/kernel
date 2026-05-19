# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Org-units test fixtures.

Installs the is_internal consistency trigger on the test database after the
session-scoped schema provisioner (``_provision_test_database`` in the root
conftest) has run ``Base.metadata.create_all``.

The trigger is not part of the ORM metadata and therefore not created by
``create_all``.  It is normally installed by the Alembic migration
``2026_05_15_2331_phase_20_kn_org_units_is_internal``, but test runs bypass
migrations in favour of the create_all approach.  This conftest bridges the
gap by idempotently installing the trigger and its backing function once per
session, immediately after the schema is ready.
"""

import asyncio
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from src.inventory.org_units._trigger_sql import (
    TRIGGER_CREATE_SQL,
    TRIGGER_DROP_IF_EXISTS,
    TRIGGER_FUNC_SQL,
)


def _get_test_db_url() -> str:
    """Resolve the test database URL using the same logic as the root conftest."""
    import os  # noqa: PLC0415
    from urllib.parse import urlparse, urlunparse  # noqa: PLC0415

    from dotenv import load_dotenv  # noqa: PLC0415

    load_dotenv()
    try:
        from src.core.config import get_settings  # noqa: PLC0415

        dsn = get_settings().postgres.dsn
    except Exception:  # noqa: BLE001 # allowed-broad: test fixture cleanup
        dsn = os.getenv('DATABASE_URL') or ''
        if not dsn:
            raise

    parsed = urlparse(dsn)
    db_name = parsed.path.lstrip('/')
    test_db = db_name.rsplit('_', 1)[0] + '_test' if '_' in db_name else db_name + '_test'
    return urlunparse(parsed._replace(path='/' + test_db))


@pytest.fixture(scope='session', autouse=True)
def _install_is_internal_trigger(_provision_test_database: None) -> Iterator[None]:
    """Install the is_internal consistency trigger once per test session.

    Runs after ``_provision_test_database`` (declared as explicit dependency)
    so the ``org_units`` table exists when we create the trigger.
    The fixture is idempotent: ``CREATE OR REPLACE FUNCTION`` and
    ``DROP TRIGGER IF EXISTS`` make repeated installs safe.
    """

    async def _install() -> None:
        engine = create_async_engine(_get_test_db_url(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(sa.text(TRIGGER_FUNC_SQL))
                await conn.execute(sa.text(TRIGGER_DROP_IF_EXISTS))
                await conn.execute(sa.text(TRIGGER_CREATE_SQL))
        finally:
            await engine.dispose()

    asyncio.run(_install())
    yield

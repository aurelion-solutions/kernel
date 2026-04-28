# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import asyncio
import importlib
from logging.config import fileConfig
from pathlib import Path
import sys

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

load_dotenv()

from src.platform.secrets.factory import register_default_providers  # noqa: E402

register_default_providers()
from alembic import context  # noqa: E402
from sqlalchemy import pool  # noqa: E402
from sqlalchemy.engine import Connection  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402
from src.core.config import get_settings  # noqa: E402
from src.core.db.base import Base  # noqa: E402


def import_all_model_modules() -> None:
    import src.capabilities
    import src.inventory
    import src.platform

    for pkg in (src.inventory, src.capabilities, src.platform):
        for root in map(Path, pkg.__path__):
            for path in root.rglob('models.py'):
                rel = path.relative_to(PROJECT_ROOT)
                module_name = '.'.join(rel.with_suffix('').parts)
                importlib.import_module(module_name)


import_all_model_modules()

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_db_url() -> str:
    """Resolve the database URL from config or bootstrap settings."""
    url = config.get_main_option('sqlalchemy.url') or ''
    # alembic.ini may store a placeholder `""` — strip surrounding quotes.
    url = url.strip('"').strip("'").strip()
    if not url:
        url = get_settings().postgres.dsn
    return url


def run_migrations_offline() -> None:
    url = _resolve_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    url = _resolve_db_url()
    connectable = create_async_engine(url, poolclass=pool.NullPool)

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())

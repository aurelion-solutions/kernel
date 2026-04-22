# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from collections.abc import Iterator
import os
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
import src.capabilities.effective_access.models  # noqa: F401 — registers EffectiveGrant + partition DDL listeners
from src.core.db.base import Base
from src.core.db.deps import get_db
from src.platform.events.buffer import InMemoryEventBuffer
import src.platform.logs.models  # noqa: F401 — log_event_buffer metadata for create_all
from src.platform.logs.service import NoOpLogService
from src.routers.v0 import router

load_dotenv()


@pytest.fixture(autouse=True, scope='session')
def _default_events_provider_noop() -> Iterator[None]:
    prev = os.environ.get('AURELION_EVENTS_PROVIDER')
    os.environ['AURELION_EVENTS_PROVIDER'] = 'noop'
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop('AURELION_EVENTS_PROVIDER', None)
        else:
            os.environ['AURELION_EVENTS_PROVIDER'] = prev


DATABASE_URL = os.getenv('DATABASE_URL')
parsed = urlparse(DATABASE_URL)
db_name = parsed.path.lstrip('/')
test_db = db_name.rsplit('_', 1)[0] + '_test' if '_' in db_name else db_name + '_test'
TEST_DATABASE_URL = urlunparse(parsed._replace(path='/' + test_db))


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    try:
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )


@pytest_asyncio.fixture
async def app(engine):
    session_factory = async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
        class_=AsyncSession,
    )

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    app = FastAPI()
    app.include_router(router, prefix='/api/v0')
    app.dependency_overrides[get_db] = override_get_db
    app.state.log_service = NoOpLogService()
    app.state.event_buffer = InMemoryEventBuffer()
    return app


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url='http://testserver',
    ) as ac:
        yield ac

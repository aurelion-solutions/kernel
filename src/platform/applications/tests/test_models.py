# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

import pytest
from sqlalchemy.exc import IntegrityError
from src.platform.applications.models import Application
from src.platform.connectors.models import ConnectorInstance


@pytest.mark.asyncio
async def test_application_instantiation_with_required_fields(session_factory):
    app = Application(
        name='test-app',
        code='test-app',
        config={'queue': 'test'},
        required_connector_tags=['jira', 'eu-segment'],
        is_active=True,
    )
    assert app.name == 'test-app'
    assert app.config == {'queue': 'test'}
    assert app.required_connector_tags == ['jira', 'eu-segment']
    assert app.is_active is True


@pytest.mark.asyncio
async def test_application_persists_and_loads(session_factory):
    async with session_factory() as session:
        app = Application(
            name='persist-test',
            code='persist-test',
            config={'key': 'value'},
            required_connector_tags=['jira'],
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        loaded = await session.get(Application, app_id)
        assert loaded is not None
        assert loaded.name == 'persist-test'
        assert loaded.config == {'key': 'value'}
        assert loaded.required_connector_tags == ['jira']


@pytest.mark.asyncio
async def test_is_active_defaults_and_toggle(session_factory):
    async with session_factory() as session:
        app = Application(
            name='active-test',
            code='active-test',
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        loaded = await session.get(Application, app_id)
        assert loaded.is_active is True
        loaded.is_active = False
        await session.commit()

    async with session_factory() as session:
        loaded = await session.get(Application, app_id)
        assert loaded.is_active is False


@pytest.mark.asyncio
async def test_config_accepts_json_dict(session_factory):
    async with session_factory() as session:
        config = {'nested': {'a': 1}, 'list': [1, 2], 'str': 'x'}
        app = Application(
            name='config-test',
            code='config-test',
            config=config,
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        loaded = await session.get(Application, app_id)
        assert loaded.config == config


@pytest.mark.asyncio
async def test_required_connector_tags_defaults_to_empty_list(session_factory):
    async with session_factory() as session:
        app = Application(
            name='tags-test',
            code='tags-test',
        )
        session.add(app)
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        loaded = await session.get(Application, app_id)
        assert loaded is not None
        assert loaded.required_connector_tags == []


@pytest.mark.asyncio
async def test_application_matching_connector_instances_filters_by_tags_and_online(
    session_factory,
):
    async with session_factory() as session:
        app = Application(
            name='match-tags-app',
            code='match-tags-app',
            required_connector_tags=['jira', 'eu'],
        )
        session.add(app)
        session.add(
            ConnectorInstance(instance_id='c-good', tags=['jira', 'eu', 'extra']),
        )
        session.add(ConnectorInstance(instance_id='c-partial', tags=['jira']))
        session.add(
            ConnectorInstance(instance_id='c-order-first', tags=['eu', 'jira']),
        )
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        loaded = await session.get(Application, app_id)
        assert loaded is not None
        matches = await loaded.matching_connector_instances(session, online_only=True)
        ids = [m.instance_id for m in matches]
        assert 'c-good' in ids
        assert 'c-order-first' in ids
        assert 'c-partial' not in ids
        assert ids == sorted(ids)


@pytest.mark.asyncio
async def test_application_matching_connector_instances_all_when_not_online_only(
    session_factory,
):
    from datetime import UTC, datetime, timedelta

    old = datetime.now(UTC) - timedelta(minutes=30)

    async with session_factory() as session:
        app = Application(
            name='match-offline-app',
            code='match-offline-app',
            required_connector_tags=['jira'],
        )
        session.add(app)
        session.add(
            ConnectorInstance(
                instance_id='offline-match',
                tags=['jira'],
                last_seen_at=old,
            ),
        )
        await session.commit()
        app_id = app.id

    async with session_factory() as session:
        loaded = await session.get(Application, app_id)
        assert loaded is not None
        online_matches = await loaded.matching_connector_instances(session, online_only=True)
        assert [m.instance_id for m in online_matches] == []
        all_matches = await loaded.matching_connector_instances(session, online_only=False)
        assert [m.instance_id for m in all_matches] == ['offline-match']


@pytest.mark.asyncio
async def test_application_code_unique_constraint(session_factory):
    """Two applications with the same code raise IntegrityError."""
    async with session_factory() as session:
        app1 = Application(name='dup-code-1', code='dup-code')
        app2 = Application(name='dup-code-2', code='dup-code')
        session.add(app1)
        session.add(app2)
        with pytest.raises(IntegrityError):
            await session.commit()


@pytest.mark.asyncio
async def test_application_code_not_null(session_factory):
    """Application without code raises IntegrityError on commit."""
    async with session_factory() as session:
        app = Application(name='no-code-app', code=None)  # type: ignore[arg-type]
        session.add(app)
        with pytest.raises(IntegrityError):
            await session.commit()

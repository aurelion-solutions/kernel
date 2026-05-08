# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Integration tests for the CapabilityGrant repository layer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import uuid

import pytest
from src.inventory.access_model.capability_grants.capability_projector import CapabilityGrantDraft
from src.inventory.access_model.capability_grants.repository import (
    count_grants_for_mapping,
    list_capability_grants,
    tombstone_capability_grants_for_effective_grant,
    upsert_capability_grants,
)
from src.inventory.access_model.capability_grants.tests import _seed_minimal_refs

_NOW = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)


def _draft(
    subject_id,
    capability_id,
    scope_key_id,
    application_id,
    source_eg_id,
    mapping_id,
    *,
    scope_value: str | None = None,
    observed_at: datetime = _NOW,
    tombstoned_at: datetime | None = None,
) -> CapabilityGrantDraft:
    return CapabilityGrantDraft(
        subject_id=subject_id,
        capability_id=capability_id,
        scope_key_id=scope_key_id,
        scope_value=scope_value,
        application_id=application_id,
        source_effective_grant_id=source_eg_id,
        source_capability_mapping_id=mapping_id,
        observed_at=observed_at,
        tombstoned_at=tombstoned_at,
    )


@pytest.mark.asyncio
async def test_upsert_inserts_new_grants_and_returns_counts(session_factory) -> None:
    """Empty table → 3 drafts → rows_inserted=3, rows_updated=0."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)

        sid, cid, skid, appid, mid = (
            refs.subject_id,
            refs.capability_id,
            refs.scope_key_global_id,
            refs.application_id,
            refs.mapping_id,
        )
        drafts = [
            _draft(sid, cid, skid, appid, uuid.uuid4(), mid),
            _draft(sid, cid, skid, appid, uuid.uuid4(), mid),
            _draft(sid, cid, skid, appid, uuid.uuid4(), mid),
        ]

        result = await upsert_capability_grants(session, drafts)
        await session.flush()

        assert result.rows_upserted == 3
        assert result.rows_inserted == 3
        assert result.rows_updated == 0
        assert result.rows_tombstoned == 0


@pytest.mark.asyncio
async def test_upsert_on_conflict_updates_observed_and_tombstoned_but_not_application_id(session_factory) -> None:
    """Upsert with a different application_id does NOT overwrite the stored application_id.

    This test guards the application_id immutability invariant.
    """
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)

        # Create a second application for the "wrong" app_id
        from src.platform.applications.models import Application

        app2 = Application(
            name=f'app2-{uuid.uuid4().hex[:8]}',
            code=f'code2-{uuid.uuid4().hex[:8]}',
            config={},
            required_connector_tags=[],
            is_active=True,
        )
        session.add(app2)
        await session.flush()

        eg_id = uuid.uuid4()
        original_app_id = refs.application_id
        t1 = _NOW
        t2 = _NOW + timedelta(hours=1)
        tombstone_ts = _NOW + timedelta(hours=2)

        # First insert with original application_id
        draft1 = _draft(
            refs.subject_id,
            refs.capability_id,
            refs.scope_key_global_id,
            original_app_id,
            eg_id,
            refs.mapping_id,
            observed_at=t1,
        )
        await upsert_capability_grants(session, [draft1])
        await session.flush()

        # Re-upsert same source pair but with different application_id and tombstoned
        draft2 = _draft(
            refs.subject_id,
            refs.capability_id,
            refs.scope_key_global_id,
            app2.id,  # synthetic different app_id — immutability invariant should prevent overwrite
            eg_id,
            refs.mapping_id,
            observed_at=t2,
            tombstoned_at=tombstone_ts,
        )
        result2 = await upsert_capability_grants(session, [draft2])
        await session.flush()

        assert result2.rows_updated == 1
        assert result2.rows_inserted == 0

        # Verify application_id was NOT overwritten
        import sqlalchemy as sa
        from src.inventory.access_model.capability_grants.models import CapabilityGrant

        row = (
            (
                await session.execute(
                    sa.select(CapabilityGrant).where(
                        CapabilityGrant.source_effective_grant_id == eg_id,
                        CapabilityGrant.source_capability_mapping_id == refs.mapping_id,
                    )
                )
            )
            .scalars()
            .one()
        )

        assert row.application_id == original_app_id  # must NOT be app2.id
        assert row.observed_at == t2  # updated
        assert row.tombstoned_at == tombstone_ts  # updated


@pytest.mark.asyncio
async def test_tombstone_capability_grants_for_effective_grant_idempotent(session_factory) -> None:
    """Call tombstone twice with same observed_at; second call returns 0."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)

        eg_id = uuid.uuid4()
        t_insert = _NOW - timedelta(hours=1)
        t_tombstone = _NOW

        draft = _draft(
            refs.subject_id,
            refs.capability_id,
            refs.scope_key_global_id,
            refs.application_id,
            eg_id,
            refs.mapping_id,
            observed_at=t_insert,
        )
        await upsert_capability_grants(session, [draft])
        await session.flush()

        count1 = await tombstone_capability_grants_for_effective_grant(
            session, effective_grant_id=eg_id, observed_at=t_tombstone
        )
        assert count1 == 1

        # Second call — already tombstoned, guard predicate (tombstoned_at IS NULL OR tombstoned_at > now) fails
        count2 = await tombstone_capability_grants_for_effective_grant(
            session, effective_grant_id=eg_id, observed_at=t_tombstone
        )
        assert count2 == 0


@pytest.mark.asyncio
async def test_count_grants_for_mapping_excludes_tombstoned(session_factory) -> None:
    """Two active + one tombstoned for the same mapping; count returns 2."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)

        t_active = _NOW - timedelta(hours=2)
        t_tombstone = _NOW - timedelta(hours=1)

        active1 = _draft(
            refs.subject_id,
            refs.capability_id,
            refs.scope_key_global_id,
            refs.application_id,
            uuid.uuid4(),
            refs.mapping_id,
            observed_at=t_active,
        )
        active2 = _draft(
            refs.subject_id,
            refs.capability_id,
            refs.scope_key_global_id,
            refs.application_id,
            uuid.uuid4(),
            refs.mapping_id,
            observed_at=t_active,
        )
        tombstoned = _draft(
            refs.subject_id,
            refs.capability_id,
            refs.scope_key_global_id,
            refs.application_id,
            uuid.uuid4(),
            refs.mapping_id,
            observed_at=t_active,
            tombstoned_at=t_tombstone,
        )

        await upsert_capability_grants(session, [active1, active2, tombstoned])
        await session.flush()

        count = await count_grants_for_mapping(session, refs.mapping_id)
        assert count == 2


@pytest.mark.asyncio
async def test_list_filters_combine_correctly(session_factory) -> None:
    """Seed several rows; filter by (subject_id, capability_id, active_only=True) → expected subset."""
    async with session_factory() as session:
        refs = await _seed_minimal_refs(session)

        t_active = _NOW - timedelta(hours=1)
        t_tombstone = _NOW

        active = _draft(
            refs.subject_id,
            refs.capability_id,
            refs.scope_key_global_id,
            refs.application_id,
            uuid.uuid4(),
            refs.mapping_id,
            observed_at=t_active,
        )
        tombstoned = _draft(
            refs.subject_id,
            refs.capability_id,
            refs.scope_key_global_id,
            refs.application_id,
            uuid.uuid4(),
            refs.mapping_id,
            observed_at=t_active,
            tombstoned_at=t_tombstone,
        )

        await upsert_capability_grants(session, [active, tombstoned])
        await session.flush()

        result = await list_capability_grants(
            session,
            subject_id=refs.subject_id,
            capability_id=refs.capability_id,
            active_only=True,
            now=_NOW,
        )
        assert len(result) == 1
        assert result[0].tombstoned_at is None

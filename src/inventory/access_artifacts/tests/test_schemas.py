# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Schema unit tests for AccessArtifact permitted universal fields (Phase 12 Step 9)."""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

import pytest
from src.inventory.access_artifacts.schemas import AccessArtifactCreate, AccessArtifactRead


@pytest.mark.asyncio
async def test_access_artifact_create_accepts_all_four_permitted_fields() -> None:
    """AccessArtifactCreate validates successfully with all four permitted fields set."""
    schema = AccessArtifactCreate(
        application_id=uuid.uuid4(),
        artifact_type='sap_role',
        external_id='role-001',
        payload={'name': 'ADMIN'},
        raw_name='SAP ADMIN Role',
        effect='grant',
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2026, 12, 31, tzinfo=UTC),
    )
    assert schema.raw_name == 'SAP ADMIN Role'
    assert schema.effect == 'grant'
    assert schema.valid_from == datetime(2026, 1, 1, tzinfo=UTC)
    assert schema.valid_until == datetime(2026, 12, 31, tzinfo=UTC)


@pytest.mark.asyncio
async def test_access_artifact_create_defaults_four_fields_to_none() -> None:
    """AccessArtifactCreate defaults all four permitted fields to None when omitted."""
    schema = AccessArtifactCreate(
        application_id=uuid.uuid4(),
        artifact_type='acl_entry',
        external_id='acl-001',
        payload={},
    )
    assert schema.raw_name is None
    assert schema.effect is None
    assert schema.valid_from is None
    assert schema.valid_until is None


@pytest.mark.asyncio
async def test_access_artifact_read_validates_with_four_fields_set() -> None:
    """AccessArtifactRead validates successfully when all four permitted fields are present."""
    now = datetime.now(UTC)
    schema = AccessArtifactRead(
        id=uuid.uuid4(),
        application_id=uuid.uuid4(),
        artifact_type='db_grant',
        external_id='grant-001',
        payload={'privilege': 'SELECT'},
        raw_name='db_grant_select',
        effect='permit',
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        valid_until=datetime(2026, 6, 30, tzinfo=UTC),
        ingested_at=now,
        ingest_batch_id='batch-001',
        observed_at=now,
        is_active=True,
        tombstoned_at=None,
    )
    assert schema.raw_name == 'db_grant_select'
    assert schema.effect == 'permit'
    assert schema.valid_from is not None
    assert schema.valid_until is not None


@pytest.mark.asyncio
async def test_access_artifact_read_validates_with_four_fields_null() -> None:
    """AccessArtifactRead validates successfully when all four permitted fields are None."""
    now = datetime.now(UTC)
    schema = AccessArtifactRead(
        id=uuid.uuid4(),
        application_id=uuid.uuid4(),
        artifact_type='acl_entry',
        external_id='acl-002',
        payload={},
        raw_name=None,
        effect=None,
        valid_from=None,
        valid_until=None,
        ingested_at=now,
        ingest_batch_id=None,
        observed_at=now,
        is_active=True,
        tombstoned_at=None,
    )
    assert schema.raw_name is None
    assert schema.effect is None
    assert schema.valid_from is None
    assert schema.valid_until is None

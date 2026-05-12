# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Tests: batch_size setting propagated to _phase_load_artifacts (Phase 17 Step 14)."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from src.engines.inventory_reconcile.pipeline import _phase_load_artifacts
from src.platform.lake.duckdb_session import LakeSession


@pytest.mark.asyncio
async def test_phase_load_artifacts_uses_settings_batch_size():
    """_phase_load_artifacts calls fetchmany with the supplied batch_size."""
    application_id = uuid4()
    batch_size = 7

    # Build a minimal LakeSession mock that returns no rows
    lake_session = MagicMock(spec=LakeSession)
    lake_session.iceberg_table_path = MagicMock(return_value='test_path')
    lake_session.execute = MagicMock()

    # _conn.fetchmany is called in the while loop
    lake_session._conn = MagicMock()
    lake_session._conn.fetchmany = MagicMock(return_value=[])  # empty → loop exits immediately

    _phase_load_artifacts(lake_session, application_id=application_id, batch_size=batch_size)

    lake_session._conn.fetchmany.assert_called_once_with(batch_size)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Slice-local DTOs for reconciliation Iceberg reads.

These are NOT reused from ``capabilities/access_analysis/detectors/unused.py``.
That module's ``AccessFactView`` is detector-shaped (carries ``last_seen``,
lacks ``effect``/``valid_until``/``is_active``/``natural_key_hash``).
Reconciliation needs different projections — hence separate DTOs here.

No SQLAlchemy imports in this file.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import ConfigDict
from pydantic.dataclasses import dataclass


@dataclass(config=ConfigDict(frozen=True, extra='forbid'))
class AccessArtifactRowView:
    """Projection of ``raw.access_artifacts`` columns used by reconciliation.

    Excluded columns (not read by handlers or delta logic):
    - ``tombstoned_at``  — lifecycle column owned by ingest
    - ``ingested_at``    — write-time metadata, irrelevant to set-diff
    """

    id: UUID
    application_id: UUID
    artifact_type: str
    external_id: str
    payload: str | None
    raw_name: str | None
    effect: str | None
    valid_from: datetime | None
    valid_until: datetime | None
    is_active: bool
    observed_at: datetime
    ingest_batch_id: UUID | None


@dataclass(config=ConfigDict(frozen=True, extra='forbid'))
class AccessFactRowView:
    """Projection of ``normalized.access_facts`` columns used by reconciliation.

    Excluded columns (never read by delta logic):
    - ``created_at``           — provenance metadata
    - ``revoked_at``           — provenance metadata
    - ``latest_batch_id``      — ingest ledger reference
    - denorm columns           — partition helpers, not part of the natural key
    - ``reconciliation_delta_item_id`` — write-time reference, not read back
    """

    id: UUID
    subject_id: UUID | None
    account_id: UUID | None
    resource_id: UUID
    action_id: int
    effect: str
    valid_from: datetime | None
    valid_until: datetime | None
    is_active: bool
    observed_at: datetime | None
    natural_key_hash: str

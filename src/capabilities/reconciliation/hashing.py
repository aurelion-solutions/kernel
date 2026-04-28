# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Natural-key hash helper for reconciliation + lake migration slices.

Extracted from ``pipeline.py`` (D5 — Phase 15 Step 14 architect decision).

Formula: SHA-256 hex of the canonical 6-field string::

    <app_id>|<subject_id>|<account_id_or_NULL>|<resource_id>|<action_id>|<effect>

Null-safety: NULL ``account_id`` is encoded as the ``\\x00`` sentinel (not empty
string). Empty string and NULL produce different encodings — ``\\x00`` cannot be
confused with any real UUID because UUID characters are limited to ``[0-9a-f-]``.

Separator safety: ``|`` cannot appear in any field value (UUID: ``[0-9a-f-]``;
``action_id``: BIGINT digits only; ``effect``: controlled vocabulary, no pipe).
"""

from __future__ import annotations

import hashlib
from uuid import UUID


def compute_natural_key_hash(
    app_id: UUID,
    subject_id: UUID,
    account_id: UUID | None,
    resource_id: UUID,
    action_id: int,
    effect: str,
) -> str:
    """Return the SHA-256 hex digest of the canonical 6-field natural key.

    Args:
        app_id:      Application UUID (prevents cross-application collisions).
        subject_id:  Subject UUID (globally unique; kind not included — see §4.2 note).
        account_id:  Account UUID, or ``None``; encoded as ``\\x00`` when absent.
        resource_id: Resource UUID.
        action_id:   Action BIGINT id.
        effect:      Effect string from controlled vocabulary (``allow``, ``deny``, …).

    Returns:
        64-character lowercase hex string (SHA-256 digest).
    """
    account_part = str(account_id) if account_id is not None else '\x00'
    raw = f'{app_id}|{subject_id}|{account_part}|{resource_id}|{action_id}|{effect}'
    return hashlib.sha256(raw.encode()).hexdigest()

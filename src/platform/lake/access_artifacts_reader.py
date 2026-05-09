# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Physical DuckDB read path for ``raw.access_artifacts``.

Public entry points:
  - :func:`run_iceberg_scan` — cursor-paginated DuckDB iceberg_scan.
  - :func:`run_get_by_id` — single-row fetch by artifact id.

Row shape:
  - Returns :class:`AccessArtifactRow` typed dataclass (lake-level row shape, not domain DTO).
  - :data:`SCAN_COLUMNS` is the public column-order contract — the façade uses it
    to build the ``AccessArtifactRow`` → ``AccessArtifactView`` mapping without
    re-declaring the column list.

Cursor codec:
  - :func:`encode_cursor` / :func:`decode_cursor` — base64url opaque cursor for
    the ``id``-keyed iceberg_scan pagination.
  - :class:`InvalidCursorError` — raised when a cursor token is malformed.

Library module — MUST NOT call load_dotenv() / get_settings() at import time.
Forbidden imports: src.inventory.*, src.engines.*, src.products.*,
                   pyiceberg, pyarrow (DuckDB only via lake_session.execute).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
from typing import Any
import uuid

# ---------------------------------------------------------------------------
# Error class
# ---------------------------------------------------------------------------


class InvalidCursorError(Exception):
    """Raised when a pagination cursor token is malformed or undecodable."""

    def __init__(self, detail: str = 'invalid cursor') -> None:
        super().__init__(detail)


# ---------------------------------------------------------------------------
# Lake-level row shape (NOT a domain DTO — no Pydantic, no inventory imports)
# ---------------------------------------------------------------------------

#: Ordered column names for the DuckDB iceberg_scan SELECT.
#: This is the public column-order contract — the façade maps AccessArtifactRow → AccessArtifactView.
SCAN_COLUMNS: tuple[str, ...] = (
    'id',
    'application_id',
    'artifact_type',
    'external_id',
    'payload',
    'raw_name',
    'effect',
    'valid_from',
    'valid_until',
    'ingested_at',
    'ingest_batch_id',
    'observed_at',
    'is_active',
    'tombstoned_at',
)


@dataclass(frozen=True, slots=True)
class AccessArtifactRow:
    """Lake-level row returned by the reader.

    Field names and order match :data:`SCAN_COLUMNS`.
    This is a typed boundary sentinel — NOT a domain DTO (no Pydantic, not named
    after a domain concept). The façade maps this to ``AccessArtifactView``.
    """

    id: Any
    application_id: Any
    artifact_type: Any
    external_id: Any
    payload: Any
    raw_name: Any
    effect: Any
    valid_from: Any
    valid_until: Any
    ingested_at: Any
    ingest_batch_id: Any
    observed_at: Any
    is_active: Any
    tombstoned_at: Any


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def encode_cursor(last_seen_id: str) -> str:
    """Encode last_seen_id into an opaque base64url cursor token."""
    token = json.dumps({'last_seen_id': last_seen_id}, separators=(',', ':'))
    return base64.urlsafe_b64encode(token.encode()).decode().rstrip('=')


def decode_cursor(cursor: str) -> str:
    """Decode a base64url cursor token and return last_seen_id.

    Raises:
        :class:`InvalidCursorError` if the cursor is malformed or missing
        the ``last_seen_id`` key.
    """
    try:
        padded = cursor + '=' * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        data = json.loads(raw)
        last_seen_id = data['last_seen_id']
        if not isinstance(last_seen_id, str):
            raise InvalidCursorError()
        return last_seen_id
    except (KeyError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError() from exc


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _row_tuple_to_row(row: tuple[Any, ...]) -> AccessArtifactRow:
    """Convert a DuckDB fetchall tuple to a typed :class:`AccessArtifactRow`."""
    return AccessArtifactRow(**dict(zip(SCAN_COLUMNS, row, strict=True)))


# ---------------------------------------------------------------------------
# Public entry points (sync — caller wraps in asyncio.to_thread)
# ---------------------------------------------------------------------------


def run_iceberg_scan(
    lake_session: Any,
    *,
    warehouse_uri: str,
    application_id: uuid.UUID | None,
    artifact_type: str | None,
    is_active: bool | None,
    last_seen_id: str | None,
    fetch_size: int,
) -> list[AccessArtifactRow]:
    """Execute DuckDB iceberg_scan query.

    Sync — must be called via ``asyncio.to_thread`` from async context.

    Returns:
        List of :class:`AccessArtifactRow` matching the given filters.
        The list may contain up to ``fetch_size`` rows; if it equals ``fetch_size``
        the caller should assume there are more rows available.
    """
    predicates: list[str] = []
    params: list[Any] = []

    if application_id is not None:
        predicates.append('application_id = ?')
        params.append(str(application_id))

    if last_seen_id is not None:
        predicates.append('id > ?')
        params.append(last_seen_id)

    if is_active is not None:
        predicates.append('is_active = ?')
        params.append(is_active)

    if artifact_type is not None:
        predicates.append('artifact_type = ?')
        params.append(artifact_type)

    where_clause = ('WHERE ' + ' AND '.join(predicates)) if predicates else ''

    col_list = ', '.join(SCAN_COLUMNS)
    sql = f"""
        SELECT {col_list}
        FROM iceberg_scan('{warehouse_uri}/raw/access_artifacts')
        {where_clause}
        ORDER BY id
        LIMIT ?
    """
    params.append(fetch_size)

    lake_session.execute(sql, params)
    rows_raw: list[tuple[Any, ...]] = lake_session.fetchall()

    return [_row_tuple_to_row(row) for row in rows_raw]


def run_get_by_id(
    lake_session: Any,
    *,
    warehouse_uri: str,
    artifact_id: uuid.UUID,
) -> AccessArtifactRow | None:
    """Fetch single artifact by id via DuckDB.

    Sync — must be called via ``asyncio.to_thread`` from async context.

    Returns:
        :class:`AccessArtifactRow` or ``None`` if not found.
    """
    col_list = ', '.join(SCAN_COLUMNS)
    sql = f"""
        SELECT {col_list}
        FROM iceberg_scan('{warehouse_uri}/raw/access_artifacts')
        WHERE id = ?::uuid
        LIMIT 1
    """
    lake_session.execute(sql, [str(artifact_id)])
    row = lake_session.fetchone()
    if row is None:
        return None
    return _row_tuple_to_row(row)

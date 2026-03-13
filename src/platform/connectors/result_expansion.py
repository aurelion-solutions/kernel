# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

from collections.abc import Iterable
from typing import Any

from src.platform.storage.factory import DataLakeStorageFactory


def expand_records_from_response(
    response: dict[str, Any],
    *,
    list_key: str,
    lake_factory: DataLakeStorageFactory,
) -> dict[str, Any]:
    """Turn a successful connector RPC envelope into ``{list_key: [records...]}``.

    Handles inline ``payload`` (object with ``list_key``, bare list, or passthrough dict)
    and ``result_storage_ref`` (batch-read via ``lake_factory``).
    """
    result_storage_ref = response.get('result_storage_ref')
    if isinstance(result_storage_ref, dict):
        provider = result_storage_ref.get('provider')
        storage_key = result_storage_ref.get('storage_key')
        if not provider or not storage_key:
            raise ValueError('result_storage_ref requires provider and storage_key')
        storage = lake_factory.get(provider)
        records = list(storage.read_batch(storage_key))
        return {list_key: _normalize_records(records)}

    payload = response.get('payload')
    if isinstance(payload, dict):
        if list_key in payload and isinstance(payload[list_key], list):
            return {list_key: _normalize_records(payload[list_key])}
        return payload

    if isinstance(payload, list):
        return {list_key: _normalize_records(payload)}

    raise ValueError('Connector result must contain payload or result_storage_ref')


def _normalize_records(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in records]

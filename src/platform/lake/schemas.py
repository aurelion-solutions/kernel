# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""PyIceberg schema and partition spec declarations for lake tables.

Constants are declared here; Step 2's ``ensure_tables`` calls ``catalog.create_table()``.
No catalog mutations in this module.
"""

from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import (
    BooleanType,
    NestedField,
    StringType,
    TimestamptzType,
    UUIDType,
)

# ---------------------------------------------------------------------------
# Namespace + table name constants
# ---------------------------------------------------------------------------

RAW_NAMESPACE: tuple[str, ...] = ('raw',)
NORMALIZED_NAMESPACE: tuple[str, ...] = ('normalized',)
RAW_ACCESS_ARTIFACTS_TABLE: tuple[str, ...] = ('raw', 'access_artifacts')
NORMALIZED_ACCESS_FACTS_TABLE: tuple[str, ...] = ('normalized', 'access_facts')

# ---------------------------------------------------------------------------
# raw.access_artifacts
# ---------------------------------------------------------------------------

RAW_ACCESS_ARTIFACTS_SCHEMA = Schema(
    NestedField(field_id=1, name='id', field_type=UUIDType(), required=True),
    NestedField(field_id=2, name='application_id', field_type=UUIDType(), required=True),
    NestedField(field_id=3, name='artifact_type', field_type=StringType(), required=True),
    NestedField(field_id=4, name='external_id', field_type=StringType(), required=True),
    NestedField(field_id=5, name='payload', field_type=StringType(), required=False),
    NestedField(field_id=6, name='raw_name', field_type=StringType(), required=False),
    NestedField(field_id=7, name='effect', field_type=StringType(), required=False),
    NestedField(field_id=8, name='valid_from', field_type=TimestamptzType(), required=False),
    NestedField(field_id=9, name='valid_until', field_type=TimestamptzType(), required=False),
    NestedField(field_id=10, name='is_active', field_type=BooleanType(), required=True),
    NestedField(field_id=11, name='tombstoned_at', field_type=TimestamptzType(), required=False),
    NestedField(field_id=12, name='observed_at', field_type=TimestamptzType(), required=True),
    NestedField(field_id=13, name='ingested_at', field_type=TimestamptzType(), required=True),
    NestedField(field_id=14, name='ingest_batch_id', field_type=UUIDType(), required=False),
)

RAW_ACCESS_ARTIFACTS_PARTITION_SPEC = PartitionSpec(
    PartitionField(
        source_id=2,
        field_id=1000,
        transform=IdentityTransform(),
        name='application_id',
    ),
    PartitionField(
        source_id=3,
        field_id=1001,
        transform=IdentityTransform(),
        name='artifact_type',
    ),
)

# ---------------------------------------------------------------------------
# normalized.access_facts
# ---------------------------------------------------------------------------

RAW_NORMALIZED_ACCESS_FACTS_SCHEMA_FIELDS = [
    NestedField(field_id=1, name='id', field_type=UUIDType(), required=True),
    NestedField(field_id=2, name='subject_id', field_type=UUIDType(), required=True),
    NestedField(field_id=3, name='account_id', field_type=UUIDType(), required=False),
    NestedField(field_id=4, name='resource_id', field_type=UUIDType(), required=True),
    NestedField(field_id=5, name='action_id', field_type=StringType(), required=True),
    NestedField(field_id=6, name='effect', field_type=StringType(), required=True),
    NestedField(field_id=7, name='valid_from', field_type=TimestamptzType(), required=True),
    NestedField(field_id=8, name='valid_until', field_type=TimestamptzType(), required=False),
    NestedField(field_id=9, name='is_active', field_type=BooleanType(), required=True),
    NestedField(field_id=10, name='observed_at', field_type=TimestamptzType(), required=True),
    NestedField(field_id=11, name='created_at', field_type=TimestamptzType(), required=True),
    NestedField(field_id=12, name='revoked_at', field_type=TimestamptzType(), required=False),
    NestedField(field_id=13, name='latest_batch_id', field_type=UUIDType(), required=False),
    NestedField(field_id=14, name='application_id_denorm', field_type=UUIDType(), required=True),
    NestedField(field_id=15, name='subject_kind_denorm', field_type=StringType(), required=True),
    NestedField(field_id=16, name='reconciliation_delta_item_id', field_type=UUIDType(), required=True),
    NestedField(field_id=17, name='natural_key_hash', field_type=StringType(), required=True),
]

NORMALIZED_ACCESS_FACTS_SCHEMA = Schema(*RAW_NORMALIZED_ACCESS_FACTS_SCHEMA_FIELDS)

NORMALIZED_ACCESS_FACTS_PARTITION_SPEC = PartitionSpec(
    PartitionField(
        source_id=14,
        field_id=1000,
        transform=IdentityTransform(),
        name='application_id_denorm',
    ),
    PartitionField(
        source_id=15,
        field_id=1001,
        transform=IdentityTransform(),
        name='subject_kind_denorm',
    ),
)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Lake batch metadata model for data lake batch references."""

from datetime import datetime
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.db.base import Base


class LakeBatch(Base):
    """Metadata for a batch written to the data lake. Payload lives in lake backend."""

    __tablename__ = 'lake_batches'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    storage_provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    dataset_type: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey('applications.id', ondelete='SET NULL'),
        nullable=True,
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    content_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    iceberg_namespace: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment='PyIceberg namespace (e.g. `raw`, `normalized`); NULL for legacy file-based batches.',
    )
    iceberg_table: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment='PyIceberg table name within the namespace; NULL for legacy file-based batches.',
    )
    snapshot_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment='Iceberg snapshot id returned by PyIceberg commit; NULL for legacy file-based batches.',
    )

    __table_args__ = (
        sa.Index(
            'uq_lake_batches_storage_provider_storage_key_active',
            'storage_provider',
            'storage_key',
            unique=True,
            postgresql_where=sa.text('storage_provider IS NOT NULL AND storage_key IS NOT NULL'),
        ),
    )

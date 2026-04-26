# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""LLMModel ORM, LLMProvider enum, and LLMExecutionProfile ORM.

The ``llm_provider`` Postgres enum is OWNED by this slice. The migration
(``2026_04_25_0000_phase_14_step_02_llm_models.py``) manages its lifecycle
via explicit ``.create(bind)`` / ``.drop(bind)``. The SQLAlchemy ``Enum`` here
uses ``create_type=False`` so ``Base.metadata.create_all`` does NOT auto-create
the type; downstream slices referencing this enum must also pass
``create_type=False``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy import Enum as SaEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func
from src.core.db.base import Base


class LLMProvider(StrEnum):
    llama_cpp = 'llama_cpp'
    openai = 'openai'
    ollama = 'ollama'


# SQLAlchemy Enum type — owned here; create_type=False because the migration
# manages the PG enum lifecycle explicitly via .create(bind).
# Downstream slices MUST also use create_type=False when referencing this enum.
_llm_provider_enum = SaEnum(
    LLMProvider,
    name='llm_provider',
    create_type=False,
    values_callable=lambda x: [e.value for e in x],
)


class LLMModel(Base):
    """ORM model for the llm_models table.

    No relationship() to Secret — cross-slice joins stay explicit
    (consistent with feedbacks/models.py, mitigations/models.py).
    """

    __tablename__ = 'llm_models'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(
        sa.String(255),
        nullable=False,
    )
    description: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )
    provider: Mapped[LLMProvider] = mapped_column(
        _llm_provider_enum,
        nullable=False,
    )
    local_path: Mapped[str | None] = mapped_column(
        sa.Text(),
        nullable=True,
    )
    endpoint_url: Mapped[str | None] = mapped_column(
        sa.String(2048),
        nullable=True,
    )
    model_ref: Mapped[str | None] = mapped_column(
        sa.String(255),
        nullable=True,
    )
    context_window: Mapped[int | None] = mapped_column(
        sa.Integer(),
        nullable=True,
    )
    max_total_tokens: Mapped[int | None] = mapped_column(
        sa.Integer(),
        nullable=True,
    )
    default_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB(),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    secret_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('secrets.id', ondelete='RESTRICT'),
        nullable=True,
    )
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean(),
        nullable=False,
        server_default=sa.text('true'),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        sa.UniqueConstraint('name', name='uq_llm_models_name'),
        sa.Index('ix_llm_models_provider', 'provider'),
        sa.Index('ix_llm_models_is_active', 'is_active'),
    )


class LLMExecutionProfile(Base):
    """ORM model for `llm_execution_profiles`.

    Cross-slice joins to `LLMModel` stay explicit (no `relationship()`),
    consistent with the existing pattern in this slice.
    """

    __tablename__ = 'llm_execution_profiles'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    name: Mapped[str] = mapped_column(
        sa.String(255),
        nullable=False,
    )
    model_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('llm_models.id', ondelete='RESTRICT'),
        nullable=False,
    )
    param_overrides: Mapped[dict[str, Any]] = mapped_column(
        JSONB(),
        nullable=False,
        server_default=sa.text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        sa.UniqueConstraint('name', name='uq_llm_execution_profiles_name'),
        sa.Index('ix_llm_execution_profiles_model_id', 'model_id'),
    )

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ORM models for the access_plan engine.

Tables:
- access_plans         — immutable plan header (append-only after creation)
- access_plan_items    — immutable plan items (operations to execute)
- access_plan_deps     — immutable item-to-item dependencies (DAG edges)
- plan_item_executions — mutable execution state per plan item
- access_apply_active  — subject-level apply lease (one active apply per subject)
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
import uuid

import sqlalchemy as sa
from sqlalchemy import DateTime, Enum, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from src.core.db.base import Base

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AccessPlanStatus(StrEnum):
    """Lifecycle status of an AccessPlan."""

    active = 'active'
    superseded = 'superseded'
    cancelled = 'cancelled'  # reserved for Phase 20 Journey UI cancel
    invalid = 'invalid'


class PlanInvalidationReason(StrEnum):
    """Why a plan was invalidated."""

    structural = 'structural'
    stale_after_apply = 'stale_after_apply'


class PlanItemKind(StrEnum):
    """The concrete operation kind a PlanItem represents."""

    account_create = 'account_create'
    account_invite = 'account_invite'
    account_activate = 'account_activate'
    account_suspend = 'account_suspend'
    account_disable = 'account_disable'
    grant_role = 'grant_role'
    revoke_role = 'revoke_role'
    group_add = 'group_add'
    group_remove = 'group_remove'
    entitlement_attach = 'entitlement_attach'
    entitlement_detach = 'entitlement_detach'


class PlanItemExecutionStatus(StrEnum):
    """Execution lifecycle for a single PlanItem."""

    proposed = 'proposed'
    executing = 'executing'
    done = 'done'
    failed = 'failed'


class PlanItemFailureReason(StrEnum):
    """Why a PlanItemExecution entered failed status."""

    precondition = 'precondition'
    apply_error = 'apply_error'
    verify_mismatch = 'verify_mismatch'
    verify_timeout = 'verify_timeout'


# ---------------------------------------------------------------------------
# AccessPlan
# ---------------------------------------------------------------------------


class AccessPlan(Base):
    """Immutable plan header — one row per planning invocation.

    After initial insert, only status / invalidation_reason /
    invalidated_by_plan_id may be mutated (auto-invalidation path).
    All other columns are append-only.
    """

    __tablename__ = 'access_plans'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    subject_ref: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment='Opaque subject identifier (employee_id or nhi_id as string)',
    )
    subject_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment='employee | nhi',
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )
    content_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment='SHA-256 of the planning input snapshot for hash-based dedup',
    )
    status: Mapped[AccessPlanStatus] = mapped_column(
        Enum(AccessPlanStatus, name='access_plan_status'),
        nullable=False,
        default=AccessPlanStatus.active,
        server_default=sa.text("'active'"),
    )
    invalidation_reason: Mapped[PlanInvalidationReason | None] = mapped_column(
        Enum(PlanInvalidationReason, name='plan_invalidation_reason'),
        nullable=True,
    )
    invalidated_by_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('access_plans.id', ondelete='SET NULL'),
        nullable=True,
    )
    requires_confirmation: Mapped[bool] = mapped_column(
        nullable=False,
        default=False,
        server_default=sa.text('false'),
        comment='True when destructive threshold exceeded; caller must pass ?confirm_destructive=true',
    )
    supersedes_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('access_plans.id', ondelete='SET NULL'),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        # Partial index for auto-invalidation query:
        # UPDATE access_plans WHERE subject_ref = X AND status = 'active'
        Index(
            'ix_access_plans_subject_ref_active',
            'subject_ref',
            'status',
            postgresql_where=sa.text("status = 'active'"),
        ),
        # Traversal of supersedes chain
        Index('ix_access_plans_supersedes_plan_id', 'supersedes_plan_id'),
        # Unique partial index for idempotency_key (only where not null)
        Index(
            'uq_access_plans_idempotency_key',
            'idempotency_key',
            unique=True,
            postgresql_where=sa.text('idempotency_key IS NOT NULL'),
        ),
    )


# ---------------------------------------------------------------------------
# PlanItem
# ---------------------------------------------------------------------------


class PlanItem(Base):
    """Immutable plan item — one concrete operation within a plan.

    Rows are append-only; never updated after creation.
    """

    __tablename__ = 'access_plan_items'

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('access_plans.id', ondelete='CASCADE'),
        nullable=False,
    )
    kind: Mapped[PlanItemKind] = mapped_column(
        Enum(PlanItemKind, name='plan_item_kind'),
        nullable=False,
    )
    application: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment='Application code / connector identifier',
    )
    account_ref: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment='Opaque account identifier in the target system, if known',
    )
    target_descriptor: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
        comment='Role / group / entitlement descriptor for the target system',
    )
    initiatives: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
        comment='Initiative objects from PDP decision (for grant path)',
    )
    initiative_refs: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
        comment='UUIDs of existing Initiative rows to close (for revoke path)',
    )
    policy_rule_refs: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sa.text("'[]'::jsonb"),
        comment='rule_id strings from PDP reasons',
    )
    decision_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sa.text("'{}'::jsonb"),
        comment='Immutable copy of PDP Decision at planning time (for audit + Phase 20 attestation UI)',
    )

    __table_args__ = (Index('ix_access_plan_items_plan_id', 'plan_id'),)


# ---------------------------------------------------------------------------
# PlanDependency
# ---------------------------------------------------------------------------


class PlanDependency(Base):
    """Immutable DAG edge: item_id must execute after requires_item_id."""

    __tablename__ = 'access_plan_deps'

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('access_plans.id', ondelete='CASCADE'),
        primary_key=True,
        nullable=False,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('access_plan_items.id', ondelete='CASCADE'),
        primary_key=True,
        nullable=False,
    )
    requires_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('access_plan_items.id', ondelete='CASCADE'),
        primary_key=True,
        nullable=False,
    )

    __table_args__ = (Index('ix_access_plan_deps_item_id', 'plan_id', 'item_id'),)


# ---------------------------------------------------------------------------
# PlanItemExecution
# ---------------------------------------------------------------------------


class PlanItemExecution(Base):
    """Mutable execution state for a single PlanItem.

    PK is (plan_id, item_id) — one row per item per plan.
    Created in 'proposed' status when a plan apply run starts.
    """

    __tablename__ = 'plan_item_executions'

    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('access_plans.id', ondelete='CASCADE'),
        primary_key=True,
        nullable=False,
    )
    item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('access_plan_items.id', ondelete='CASCADE'),
        primary_key=True,
        nullable=False,
    )
    status: Mapped[PlanItemExecutionStatus] = mapped_column(
        Enum(PlanItemExecutionStatus, name='plan_item_execution_status'),
        nullable=False,
        default=PlanItemExecutionStatus.proposed,
        server_default=sa.text("'proposed'"),
    )
    failure_reason: Mapped[PlanItemFailureReason | None] = mapped_column(
        Enum(PlanItemFailureReason, name='plan_item_failure_reason'),
        nullable=True,
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
    )

    __table_args__ = (Index('ix_plan_item_executions_plan_id', 'plan_id'),)


# ---------------------------------------------------------------------------
# AccessApplyActive  (subject-level apply lease)
# ---------------------------------------------------------------------------


class AccessApplyActive(Base):
    """Subject-level apply lease — one row per subject with an active apply.

    INSERT ... ON CONFLICT (subject_ref) DO NOTHING is the locking mechanism.
    The execute_plan action removes its row in a finally block.
    """

    __tablename__ = 'access_apply_active'

    subject_ref: Mapped[str] = mapped_column(
        String(512),
        primary_key=True,
        nullable=False,
        comment='Opaque subject identifier matching AccessPlan.subject_ref',
    )
    subject_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment='employee | nhi',
    )
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        comment='Logical link to platform_runs.id (no FK — Phase 18 schema is in a separate module)',
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey('access_plans.id', ondelete='CASCADE'),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""FeedbackService — business logic for the Feedback slice.

Events are emitted on ``aurelion.events`` (domain bus).
Feedback rows are immutable — no update or delete methods are provided.

Emit catalog:
  access_analysis.feedback.posted — on every successful create_feedback call.
"""

from __future__ import annotations

from datetime import UTC, datetime
import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.feedbacks.exceptions import (
    FeedbackCapabilityMappingNotFoundError,
    FeedbackFindingNotFoundError,
    FeedbackRuleNotFoundError,
    FeedbackSubjectNotFoundError,
    FeedbackTargetMissingError,
)
from src.capabilities.access_analysis.feedbacks.models import Feedback, FeedbackKind
from src.capabilities.access_analysis.feedbacks.repository import (
    get_feedback_by_id,
    insert_feedback,
    list_feedbacks,
    row_exists,
    subject_exists,
)
from src.capabilities.access_analysis.feedbacks.schemas import (
    FeedbackCreate,
    FeedbackRead,
)
from src.platform.events.schemas import EventEnvelope, EventParticipantKind
from src.platform.events.service import EventService

_COMPONENT = 'access_analysis.feedbacks'
_NOTE_EXCERPT_MAX = 200


def _build_feedback_posted_event(
    feedback: Feedback,
    correlation_id: str,
    actor_id: str,
    actor_kind: EventParticipantKind,
) -> EventEnvelope:
    """Build the access_analysis.feedback.posted EventEnvelope."""
    return EventEnvelope(
        event_id=uuid.uuid4(),
        event_type='access_analysis.feedback.posted',
        occurred_at=datetime.now(UTC),
        correlation_id=correlation_id,
        causation_id=None,
        payload={
            'feedback_id': str(feedback.id),
            'finding_id': str(feedback.finding_id) if feedback.finding_id is not None else None,
            'kind': feedback.kind,
            'author': feedback.created_by,
            'note_excerpt': feedback.message[:_NOTE_EXCERPT_MAX],
            'created_at': feedback.created_at.isoformat(),
        },
        actor_kind=actor_kind,
        actor_id=actor_id,
        target_kind=EventParticipantKind.SYSTEM,
        target_id=str(feedback.id),
    )


def _resolve_actor(created_by: str | None) -> tuple[EventParticipantKind, str]:
    """Resolve actor kind and id from created_by field."""
    if created_by:
        return EventParticipantKind.USER, created_by
    return EventParticipantKind.CAPABILITY, _COMPONENT


async def _validate_targets(
    session: AsyncSession,
    payload: FeedbackCreate,
) -> None:
    """Validate FK targets exist. Raises on first violation.

    Also enforces the at-least-one-target invariant (service-level mirror of the DB CHECK).
    """
    if payload.rule_id is None and payload.capability_mapping_id is None and payload.finding_id is None:
        raise FeedbackTargetMissingError()

    if payload.rule_id is not None:
        if not await row_exists(session, 'sod_rules', payload.rule_id):
            raise FeedbackRuleNotFoundError(payload.rule_id)

    if payload.capability_mapping_id is not None:
        if not await row_exists(session, 'capability_mappings', payload.capability_mapping_id):
            raise FeedbackCapabilityMappingNotFoundError(payload.capability_mapping_id)

    if payload.finding_id is not None:
        if not await row_exists(session, 'findings', payload.finding_id):
            raise FeedbackFindingNotFoundError(payload.finding_id)

    if payload.subject_id is not None:
        if not await subject_exists(session, payload.subject_id):
            raise FeedbackSubjectNotFoundError(payload.subject_id)


def _translate_create_integrity_error(exc: IntegrityError) -> None:
    """Translate IntegrityError from insert into a domain error, or re-raise.

    Maps the DB-level CHECK constraint violation on ck_feedbacks_target_required
    to FeedbackTargetMissingError. All other integrity errors are re-raised as-is.
    """
    orig = exc.orig
    asyncpg_exc = getattr(orig, '__cause__', None)
    constraint_name: str | None = getattr(asyncpg_exc, 'constraint_name', None)
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    if pgcode == '23514' and constraint_name == 'ck_feedbacks_target_required':
        raise FeedbackTargetMissingError() from None
    raise exc


class FeedbackService:
    """Service for Feedback: create + list + get.

    Depends on EventService (domain events).
    Emits domain events post-flush, pre-commit within the same session.
    Feedback is immutable — no update or delete methods exist.
    """

    def __init__(
        self,
        session: AsyncSession,
        events: EventService,
    ) -> None:
        self._session = session
        self._events = events

    async def create_feedback(
        self,
        payload: FeedbackCreate,
        *,
        correlation_id: str | None = None,
    ) -> FeedbackRead:
        """Create a new Feedback and emit feedback.posted."""
        cid = correlation_id or uuid.uuid4().hex
        await _validate_targets(self._session, payload)

        try:
            feedback = await insert_feedback(
                self._session,
                rule_id=payload.rule_id,
                capability_mapping_id=payload.capability_mapping_id,
                finding_id=payload.finding_id,
                subject_id=payload.subject_id,
                kind=payload.kind,
                message=payload.message,
                payload=payload.payload,
                created_by=payload.created_by,
            )
        except IntegrityError as exc:
            _translate_create_integrity_error(exc)
            raise  # unreachable; keeps type checker happy

        actor_kind, actor_id = _resolve_actor(payload.created_by)
        event = _build_feedback_posted_event(feedback, cid, actor_id, actor_kind)
        await self._events.emit(event)

        return FeedbackRead.model_validate(feedback)

    async def list_feedbacks(
        self,
        *,
        kind: FeedbackKind | None = None,
        rule_id: int | None = None,
        capability_mapping_id: int | None = None,
        finding_id: int | None = None,
        subject_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FeedbackRead]:
        """Return feedbacks ordered by created_at DESC, optionally filtered."""
        rows = await list_feedbacks(
            self._session,
            kind=kind,
            rule_id=rule_id,
            capability_mapping_id=capability_mapping_id,
            finding_id=finding_id,
            subject_id=subject_id,
            limit=limit,
            offset=offset,
        )
        return [FeedbackRead.model_validate(row) for row in rows]

    async def get_feedback_by_id(self, feedback_id: int) -> FeedbackRead | None:
        """Return a FeedbackRead by id, or None if not found."""
        row = await get_feedback_by_id(self._session, feedback_id)
        if row is None:
            return None
        return FeedbackRead.model_validate(row)

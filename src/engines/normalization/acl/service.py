# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""ACL Normalizer Service — orchestrates ingest + normalization pipeline."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from src.engines.normalization.acl.normalizer import normalize_acl_entry
from src.engines.normalization.acl.schemas import ACLEntryPayload, NormalizationResult
from src.inventory.access_artifacts.service import AccessArtifactService
from src.inventory.access_facts.service import (
    AccessFactService,
    DuplicateActiveAccessFactError,
)
from src.inventory.artifact_bindings.service import ArtifactBindingService
from src.inventory.resources.service import DuplicateResourceError, ResourceService
from src.platform.logs.service import LogService


class ACLNormalizerService:
    """Orchestrates the ACL ingest + normalization pipeline.

    Composes existing inventory services only. Emits NO events of its own —
    event emission is handled by each composed service.
    """

    def __init__(
        self,
        *,
        artifact_service: AccessArtifactService,
        resource_service: ResourceService,
        access_fact_service: AccessFactService,
        binding_service: ArtifactBindingService,
        log_service: LogService | None = None,
    ) -> None:
        self._artifact_service = artifact_service
        self._resource_service = resource_service
        self._access_fact_service = access_fact_service
        self._binding_service = binding_service
        # log_service is held so callers can pass the same instance to composed
        # services if needed. This orchestrator itself does NOT emit.
        self._log_service = log_service

    async def ingest_and_normalize(
        self,
        session: AsyncSession,
        *,
        application_id: uuid.UUID,
        subject_id: uuid.UUID,
        account_id: uuid.UUID | None,
        payload: ACLEntryPayload,
        artifact_external_id: str,
        ingest_batch_id: str | None = None,
        correlation_id: str | None = None,  # reserved — not propagated in Step 16
    ) -> NormalizationResult:
        """Ingest a raw ACL entry and normalize it into inventory entities.

        Sequence:
        1. Write AccessArtifact (append-only).
        2. Pure-normalize the payload.
        3. Resolve-or-create Resource (external-id idempotent).
        4. Create-or-resolve AccessFact (natural-key idempotent via SAVEPOINT).
        5. Write ArtifactBinding linking artifact → fact + resource.

        The caller owns the transaction boundary — this method does NOT commit.
        """
        # Step 1 — upsert AccessArtifact on identity triple (application_id, artifact_type, external_id).
        artifact, _ = await self._artifact_service.upsert_artifact(
            session,
            application_id=application_id,
            artifact_type='acl_entry',
            external_id=artifact_external_id,
            payload=payload.model_dump(),
            ingest_batch_id=ingest_batch_id,
        )

        # Step 2 — pure normalize (no session, no events).
        normalized = normalize_acl_entry(payload)

        # Step 3 — resolve-or-create Resource.
        # ResourceService.create_resource does NOT do an internal session.rollback(),
        # so a plain try/except is safe here — no SAVEPOINT needed.
        created_resource: bool
        existing_resource = await self._resource_service.get_resource_by_external_id(
            session,
            application_id=application_id,
            external_id=normalized.resource_external_id,
        )
        if existing_resource is not None:
            resource = existing_resource
            created_resource = False
        else:
            try:
                resource = await self._resource_service.create_resource(
                    session,
                    application_id=application_id,
                    external_id=normalized.resource_external_id,
                    kind=normalized.resource_kind,
                    privilege_level=normalized.privilege_level,
                    environment=normalized.environment,
                    data_sensitivity=normalized.data_sensitivity,
                )
                created_resource = True
            except DuplicateResourceError:
                # Lost the race — refetch.
                resource = await self._resource_service.get_resource_by_external_id(
                    session,
                    application_id=application_id,
                    external_id=normalized.resource_external_id,
                )
                assert resource is not None  # duplicate proves row exists
                created_resource = False

        # Snapshot plain UUIDs before entering SAVEPOINT so that attribute
        # access after a SAVEPOINT rollback does not trigger an ORM lazy reload
        # on a potentially-expired SQLAlchemy instance.
        artifact_id: uuid.UUID = artifact.id
        resource_id: uuid.UUID = resource.id

        # Step 4 — create-or-resolve AccessFact.
        # Wrap create_fact in a SAVEPOINT so that on DuplicateActiveAccessFactError
        # only the savepoint is rolled back; the outer transaction (AccessArtifact + Resource
        # written in steps 1–3) remains intact.
        from datetime import UTC, datetime
        from typing import Any

        # normalized.action is the Python Action StrEnum — its .value is the slug
        action_slug: str = normalized.action.value
        observed_now = datetime.now(UTC)

        fact: Any
        created_fact: bool
        try:
            async with session.begin_nested():  # SAVEPOINT
                fact = await self._access_fact_service.create_fact(
                    session,
                    delta_item_id=uuid.uuid4(),  # synthetic id; normalization pre-dates delta pipeline
                    subject_id=subject_id,
                    account_id=account_id,
                    resource_id=resource_id,
                    action_slug=action_slug,
                    effect=normalized.effect,
                    observed_at=observed_now,
                )
            created_fact = True
        except DuplicateActiveAccessFactError:
            # Active row already exists for this key; resolve it.
            # Savepoint rolled back; outer transaction (artifact + resource) intact.
            fact = await self._access_fact_service.get_fact_by_natural_key(
                session,
                subject_id=subject_id,
                account_id=account_id,
                resource_id=resource_id,
                action_slug=action_slug,
            )
            # Row must exist — duplicate exception proves it.
            assert fact is not None
            created_fact = False

        fact_id: uuid.UUID = fact.id

        # Step 5 — write ArtifactBinding (always new).
        binding = await self._binding_service.create_binding(
            session,
            artifact_id=artifact_id,
            target_type='access_fact',
            target_id=fact_id,
        )

        return NormalizationResult(
            artifact_id=artifact_id,
            resource_id=resource_id,
            access_fact_id=fact_id,
            binding_id=binding.id,
            created_fact=created_fact,
            created_resource=created_resource,
        )

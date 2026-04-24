# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""CapabilityMapping service — business logic for the CapabilityMapping slice."""

from __future__ import annotations

from typing import NoReturn
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from src.capabilities.access_analysis.capability_mappings.exceptions import (
    CapabilityMappingDefaultScopeKeyNotSeededError,
    CapabilityMappingInUseError,
    CapabilityMappingNotFoundError,
    CapabilityMappingResourceMatchExclusivityError,
    CapabilityMappingUnknownActionSlugError,
    CapabilityMappingUnknownApplicationIdError,
    CapabilityMappingUnknownCapabilityIdError,
    CapabilityMappingUnknownResourceIdError,
    CapabilityMappingUnknownScopeKeyIdError,
)
from src.capabilities.access_analysis.capability_mappings.repository import (
    delete_capability_mapping,
    get_capability_mapping_by_id,
    insert_capability_mapping,
    list_capability_mappings,
    update_capability_mapping_fields,
)
from src.capabilities.access_analysis.capability_mappings.schemas import (
    CapabilityMappingCreate,
    CapabilityMappingRead,
)
from src.platform.logs.service import LogService


def _translate_insert_integrity_error(
    exc: IntegrityError,
    *,
    capability_id: int,
    application_id: UUID | None,
    resource_id: UUID | None,
    scope_key_id: int,
) -> NoReturn:
    """Translate IntegrityError from insert/update into a domain error, or re-raise.

    asyncpg wraps the original error as exc.orig.__cause__; constraint_name lives there.
    """
    orig = exc.orig
    pgcode: str | None = getattr(orig, 'pgcode', None) or getattr(orig, 'sqlstate', None)
    asyncpg_exc = getattr(orig, '__cause__', None)
    constraint_name: str | None = getattr(asyncpg_exc, 'constraint_name', None)

    if pgcode == '23503':
        if constraint_name == 'capability_mappings_capability_id_fkey':
            raise CapabilityMappingUnknownCapabilityIdError(capability_id) from None
        if constraint_name == 'capability_mappings_application_id_fkey':
            raise CapabilityMappingUnknownApplicationIdError(application_id) from None  # type: ignore[arg-type]
        if constraint_name == 'capability_mappings_resource_id_fkey':
            raise CapabilityMappingUnknownResourceIdError(resource_id) from None  # type: ignore[arg-type]
        if constraint_name == 'capability_mappings_scope_key_id_fkey':
            raise CapabilityMappingUnknownScopeKeyIdError(scope_key_id) from None
    if pgcode == '23514' and constraint_name == 'ck_capability_mappings_resource_match_xor':
        raise CapabilityMappingResourceMatchExclusivityError() from None
    raise exc


async def resolve_default_scope_key_id(session: AsyncSession) -> int:
    """Return the id of the GLOBAL scope key. Raises CapabilityMappingDefaultScopeKeyNotSeededError if missing.

    Public helper — reused by capability_grants.service (no leading underscore).
    """
    stmt = sa.text("SELECT id FROM capability_scope_keys WHERE code = 'GLOBAL' LIMIT 1")
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        raise CapabilityMappingDefaultScopeKeyNotSeededError()
    return int(row)


async def _validate_action_slug_exists(session: AsyncSession, action_slug: str) -> None:
    """Raise CapabilityMappingUnknownActionSlugError if the slug is not in ref_actions."""
    stmt = sa.text('SELECT 1 FROM ref_actions WHERE slug = :slug LIMIT 1')
    result = await session.execute(stmt, {'slug': action_slug})
    if result.scalar_one_or_none() is None:
        raise CapabilityMappingUnknownActionSlugError(action_slug)


async def _count_dependent_capability_grants(session: AsyncSession, mapping_id: int) -> int:
    """Return count of non-tombstoned CapabilityGrant rows that reference this mapping.

    Uses a function-local import to avoid circular import at module-load time
    (capability_grants may eventually import helpers from capability_mappings).
    """
    from src.capabilities.access_analysis.capability_grants.repository import (
        count_grants_for_mapping,
    )

    return await count_grants_for_mapping(session, mapping_id)


class CapabilityMappingService:
    """CRUD service for CapabilityMapping.

    log_service is plumbed for parity with sibling slices but not used in Step 3.
    No events, no logs — per Phase 13 event catalog, mapping CRUD is not in scope.
    """

    def __init__(self, session: AsyncSession, log_service: LogService) -> None:
        self._session = session
        self._log_service = log_service

    async def create(self, payload: CapabilityMappingCreate) -> CapabilityMappingRead:
        """Create a new CapabilityMapping. Resolves GLOBAL scope key if scope_key_id is None."""
        if payload.action_slug is not None:
            await _validate_action_slug_exists(self._session, payload.action_slug)

        scope_key_id = payload.scope_key_id
        if scope_key_id is None:
            scope_key_id = await resolve_default_scope_key_id(self._session)

        scope_value_source_dict = payload.scope_value_source.model_dump(mode='json')

        try:
            mapping = await insert_capability_mapping(
                self._session,
                capability_id=payload.capability_id,
                application_id=payload.application_id,
                resource_id=payload.resource_id,
                resource_kind=payload.resource_kind,
                resource_path_glob=payload.resource_path_glob,
                action_slug=payload.action_slug,
                scope_key_id=scope_key_id,
                scope_value_source=scope_value_source_dict,
                is_active=payload.is_active,
                created_by=payload.created_by,
            )
        except IntegrityError as exc:
            _translate_insert_integrity_error(
                exc,
                capability_id=payload.capability_id,
                application_id=payload.application_id,
                resource_id=payload.resource_id,
                scope_key_id=scope_key_id,
            )

        return CapabilityMappingRead.model_validate(mapping)

    async def list(
        self,
        *,
        capability_id: int | None = None,
        application_id: UUID | None = None,
        scope_key_id: int | None = None,
        is_active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[CapabilityMappingRead]:
        """Return capability mappings with optional filters."""
        rows = await list_capability_mappings(
            self._session,
            capability_id=capability_id,
            application_id=application_id,
            scope_key_id=scope_key_id,
            is_active=is_active,
            limit=limit,
            offset=offset,
        )
        return [CapabilityMappingRead.model_validate(row) for row in rows]

    async def get(self, mapping_id: int) -> CapabilityMappingRead:
        """Return a CapabilityMapping by id. Raises CapabilityMappingNotFoundError when missing."""
        mapping = await get_capability_mapping_by_id(self._session, mapping_id)
        if mapping is None:
            raise CapabilityMappingNotFoundError(mapping_id)
        return CapabilityMappingRead.model_validate(mapping)

    async def patch(self, mapping_id: int, payload_dict: dict) -> CapabilityMappingRead:
        """Update provided fields on a CapabilityMapping.

        payload_dict must come from body.model_dump(exclude_unset=True) — only fields
        the client actually sent. capability_id, application_id, created_by are not
        patchable.
        """
        mapping = await get_capability_mapping_by_id(self._session, mapping_id)
        if mapping is None:
            raise CapabilityMappingNotFoundError(mapping_id)

        # XOR re-validation: merge patch values with current row values
        merged_resource_id = payload_dict.get('resource_id', mapping.resource_id)
        merged_resource_kind = payload_dict.get('resource_kind', mapping.resource_kind)
        merged_resource_path_glob = payload_dict.get('resource_path_glob', mapping.resource_path_glob)
        xor_count = sum(
            1 for v in (merged_resource_id, merged_resource_kind, merged_resource_path_glob) if v is not None
        )
        if xor_count != 1:
            raise CapabilityMappingResourceMatchExclusivityError()

        # action_slug re-validation
        if 'action_slug' in payload_dict and payload_dict['action_slug'] is not None:
            await _validate_action_slug_exists(self._session, payload_dict['action_slug'])

        # scope_value_source serialization — arrives as parsed Pydantic model from route layer
        kwargs: dict = {}
        for field in ('resource_id', 'resource_kind', 'resource_path_glob', 'action_slug', 'scope_key_id', 'is_active'):
            if field in payload_dict:
                kwargs[field] = payload_dict[field]

        if 'scope_value_source' in payload_dict:
            svs = payload_dict['scope_value_source']
            kwargs['scope_value_source'] = svs.model_dump(mode='json') if svs is not None else None

        try:
            mapping = await update_capability_mapping_fields(self._session, mapping, **kwargs)
        except IntegrityError as exc:
            _translate_insert_integrity_error(
                exc,
                capability_id=mapping.capability_id,
                application_id=mapping.application_id,
                resource_id=merged_resource_id,
                scope_key_id=payload_dict.get('scope_key_id', mapping.scope_key_id),
            )

        return CapabilityMappingRead.model_validate(mapping)

    async def delete(self, mapping_id: int) -> None:
        """Hard-delete a CapabilityMapping. Raises CapabilityMappingInUseError when in use."""
        mapping = await get_capability_mapping_by_id(self._session, mapping_id)
        if mapping is None:
            raise CapabilityMappingNotFoundError(mapping_id)

        grant_count = await _count_dependent_capability_grants(self._session, mapping_id)
        if grant_count > 0:
            raise CapabilityMappingInUseError(mapping_id, grant_count)

        await delete_capability_mapping(self._session, mapping)

# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""PolicyCatalogService — unified read-only catalog over SoD rules + file cartridges.

Read-only. No events. No flush/commit. Does not mutate scan or assessment
behaviour — purely a projection of existing sources.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from src.inventory.assessment.findings.models import Finding, FindingKind, FindingStatus
from src.inventory.policy.cartridges.loader import CartridgeLoadError, FileCartridgeLoader
from src.inventory.policy.cartridges.schemas import CartridgeManifest
from src.inventory.policy.catalog.schemas import (
    PolicyCatalogItem,
    PolicyCatalogResponse,
    PolicyFindingsFilter,
)
from src.inventory.policy.enums import (
    AssessmentStrategy,
    DefinitionSource,
    PolicyStatus,
    PolicyType,
)
from src.inventory.policy.sod_rules.models import SodRule
from src.platform.logs.schemas import LogLevel
from src.platform.logs.service import (
    LogService,
    NoOpLogService,
    merge_emit_log_participant_fields,
)

_COMPONENT = 'inventory.policy.catalog'
_TARGET_ID = 'policy_catalog'

# Project layout: aurelion-kernel/src/inventory/policy/catalog/service.py
# parents[5] -> monorepo root (aurelion-code) -> cartridges/lens
_DEFAULT_CARTRIDGE_ROOT: Path = Path(__file__).resolve().parents[5] / 'cartridges' / 'lens'

# MVP mapping: cartridge.id -> finding.kind. The cartridge YAML schema does
# not yet declare its emitted FindingKind directly; until that is added,
# this table is the single source of truth for catalog-side counting and
# drill-in filters. Cartridges absent from this table count as 0 findings
# and expose no findings_filter.
_CARTRIDGE_ID_TO_FINDING_KIND: dict[str, FindingKind] = {
    'lens.access_risk.orphaned_access': FindingKind.orphan_access,
    'lens.access_risk.unused_access': FindingKind.unused_access,
    'lens.access_risk.privileged_access': FindingKind.privileged_access,
    'lens.lifecycle.terminated_subject_access': FindingKind.terminated_access,
}


class PolicyCatalogService:
    """Assembles the unified policy catalog. Read-only."""

    def __init__(
        self,
        log_service: LogService | None = None,
        cartridge_root: Path | None = None,
        loader: FileCartridgeLoader | None = None,
    ) -> None:
        self._log: LogService | NoOpLogService = log_service if log_service is not None else NoOpLogService()
        self._cartridge_root = cartridge_root if cartridge_root is not None else _DEFAULT_CARTRIDGE_ROOT
        self._loader = loader if loader is not None else FileCartridgeLoader()

    async def get_catalog(self, session: AsyncSession) -> PolicyCatalogResponse:
        """Return the unified policy catalog."""
        sod_rules = await self._load_sod_rules(session)
        manifests = self._load_cartridge_manifests()

        sod_counts = await self._open_findings_by_rule_id(session)
        kind_counts = await self._open_findings_by_kind(session)

        sod_items = [_sod_rule_to_item(r, open_count=sod_counts.get(r.id, 0)) for r in sod_rules]
        cartridge_items = [
            _cartridge_to_item(
                m,
                open_count=_cartridge_open_count(m, kind_counts),
                findings_filter=_cartridge_findings_filter(m),
            )
            for m in manifests
        ]

        items = sod_items + cartridge_items
        # Stable, predictable order: type, source, name, id.
        items.sort(key=lambda i: (i.policy_type.value, i.definition_source.value, i.name, i.id))

        # allowed-emit-safe: observability
        self._log.emit_safe(
            level=LogLevel.INFO,
            message='inventory.policy.catalog.computed',
            component=_COMPONENT,
            payload=merge_emit_log_participant_fields(
                {
                    'sod_items': len(sod_items),
                    'cartridge_items': len(cartridge_items),
                    'total': len(items),
                },
                actor_component=_COMPONENT,
                target_id=_TARGET_ID,
            ),
        )
        return PolicyCatalogResponse(items=items)

    async def _load_sod_rules(self, session: AsyncSession) -> list[SodRule]:
        stmt = sa.select(SodRule).order_by(SodRule.code.asc())
        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _open_findings_by_rule_id(self, session: AsyncSession) -> dict[int, int]:
        """Return {sod_rule_id: open_finding_count} for all SoD rules."""
        stmt = (
            sa.select(Finding.rule_id, sa.func.count())
            .where(Finding.status == FindingStatus.open)
            .where(Finding.kind == FindingKind.sod)
            .where(Finding.rule_id.is_not(None))
            .group_by(Finding.rule_id)
        )
        result = await session.execute(stmt)
        return {int(rid): int(cnt) for rid, cnt in result.all() if rid is not None}

    async def _open_findings_by_kind(self, session: AsyncSession) -> dict[str, int]:
        """Return {finding_kind: open_finding_count} for non-SoD kinds."""
        stmt = (
            sa.select(Finding.kind, sa.func.count())
            .where(Finding.status == FindingStatus.open)
            .where(Finding.kind != FindingKind.sod)
            .group_by(Finding.kind)
        )
        result = await session.execute(stmt)
        return {k.value if hasattr(k, 'value') else str(k): int(cnt) for k, cnt in result.all()}

    def _load_cartridge_manifests(self) -> list[CartridgeManifest]:
        if not self._cartridge_root.exists():
            # allowed-emit-safe: best-effort warning
            self._log.emit_safe(
                level=LogLevel.WARNING,
                message='inventory.policy.catalog.cartridge_root_missing',
                component=_COMPONENT,
                payload=merge_emit_log_participant_fields(
                    {'path': str(self._cartridge_root)},
                    actor_component=_COMPONENT,
                    target_id=_TARGET_ID,
                ),
            )
            return []
        try:
            return self._loader.load_dir(self._cartridge_root)
        except CartridgeLoadError as exc:
            # allowed-emit-safe: best-effort warning
            self._log.emit_safe(
                level=LogLevel.ERROR,
                message='inventory.policy.catalog.cartridge_load_failed',
                component=_COMPONENT,
                payload=merge_emit_log_participant_fields(
                    {'path': str(self._cartridge_root), 'error': str(exc)},
                    actor_component=_COMPONENT,
                    target_id=_TARGET_ID,
                ),
            )
            return []


def _cartridge_findings_filter(manifest: CartridgeManifest) -> PolicyFindingsFilter | None:
    kind = _CARTRIDGE_ID_TO_FINDING_KIND.get(manifest.id)
    if kind is None:
        return None
    return PolicyFindingsFilter(kind=kind, rule_id=None)


def _cartridge_open_count(
    manifest: CartridgeManifest,
    kind_counts: dict[str, int],
) -> int:
    kind = _CARTRIDGE_ID_TO_FINDING_KIND.get(manifest.id)
    if kind is None:
        return 0
    return kind_counts.get(kind.value, 0)


def _cartridge_to_item(
    manifest: CartridgeManifest,
    *,
    open_count: int,
    findings_filter: PolicyFindingsFilter | None,
) -> PolicyCatalogItem:
    return PolicyCatalogItem(
        id=manifest.id,
        name=manifest.name,
        description=manifest.description,
        policy_type=manifest.policy_type,
        definition_source=DefinitionSource.FILE,
        assessment_strategy=manifest.assessment_strategy,
        status=PolicyStatus.AVAILABLE,
        version=manifest.version,
        open_findings_count=open_count,
        findings_filter=findings_filter,
    )


def _sod_rule_to_item(rule: SodRule, *, open_count: int) -> PolicyCatalogItem:
    """Project a SodRule into a PolicyCatalogItem.

    SoD rules are always policy_type=sod, definition_source=db,
    assessment_strategy=deterministic, version=None. Status reflects is_enabled.
    The findings_filter is keyed on the integer rule_id, the SoD-specific
    drill-in axis exposed by `GET /api/v0/findings`.
    """
    return PolicyCatalogItem(
        id=f'sod.rule.{rule.code}',
        name=rule.name,
        description=rule.description,
        policy_type=PolicyType.SOD,
        definition_source=DefinitionSource.DB,
        assessment_strategy=AssessmentStrategy.DETERMINISTIC,
        status=PolicyStatus.ACTIVE if rule.is_enabled else PolicyStatus.INACTIVE,
        version=None,
        open_findings_count=open_count,
        findings_filter=PolicyFindingsFilter(kind=None, rule_id=rule.id),
    )

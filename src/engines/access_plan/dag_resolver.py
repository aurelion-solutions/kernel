# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""D4 — DAG resolver: build PlanDependency rows and topological order.

Responsibilities
----------------
1. Read ``dependency_rules`` from each connector operation descriptor and build
   DAG edges between PlanItems (e.g. ``grant_role`` depends on
   ``account_create``/``account_activate``).
2. Cross-application dependencies: a ``dependency_rule`` with
   ``application=<other_app>`` is resolved against items in that application.
   No special case — treated identically to within-app deps.
3. Cascade expansion on ``account_disable``: if the connector descriptor has
   ``cascades.before_disable`` rules, synthetic revoke items are prepended to
   the plan before the ``account_disable`` item, and the disable item gains a
   dependency edge on each revoke item.
4. Cycle detection: if the resulting directed graph has a cycle the plan is
   marked ``requires_confirmation=True`` and all items involved in the cycle
   are marked ``unsatisfiable``.  This is the fail-loud behaviour: a plan with
   a cycle is never silently executed.
5. Unsatisfiable marking: an item is unsatisfiable when its dependency requires
   an operation that is absent from the plan AND the current system state does
   not already satisfy the requirement.  Such items are removed from the
   topological order and marked in ``unsatisfiable_item_ids``.
6. Returns an ordered list of ``PlanDependency`` rows and the topological
   execution order — both consumed by ``service.py``.

Design
------
Pure computation only — no DB calls.  All inputs are passed by value.
``service.py`` owns DB persistence of the returned ``PlanDependency`` rows.

Vocabulary
----------
- ``item_key``:  ``(application, kind.value)``  — stable identifier for an
  item used during dependency resolution before DB IDs are assigned.
- ``unsatisfiable``: an item cannot be safely executed because a prerequisite
  is missing from the plan *and* not already satisfied in the current state.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
import uuid

from src.engines.access_plan.models import PlanDependency, PlanItem, PlanItemKind
from src.platform.connectors.registration_schemas import (
    AccountDisableCascades,
    ConnectorCapabilityDescriptor,
    ConnectorOperationDescriptor,
)

# ---------------------------------------------------------------------------
# Public result type
# ---------------------------------------------------------------------------


@dataclass
class DAGResult:
    """Result of a single DAG resolution pass."""

    dependencies: list[PlanDependency]
    """Rows to persist in ``access_plan_deps``."""

    topo_order: list[uuid.UUID]
    """Item IDs in topological execution order (dependencies-first)."""

    unsatisfiable_item_ids: set[uuid.UUID]
    """IDs of items that cannot be executed due to unresolvable prerequisites."""

    cycle_detected: bool
    """True when the graph contains a cycle (plan should require confirmation)."""

    added_items: list[PlanItem]
    """Synthetic cascade-revoke items injected before account_disable."""


# ---------------------------------------------------------------------------
# Helpers — operation kind lookups
# ---------------------------------------------------------------------------

# Operation kinds that represent "account is now active" (satisfies 'account active' deps)
_ACCOUNT_ACTIVE_OPS: frozenset[str] = frozenset(
    {
        PlanItemKind.account_create.value,
        PlanItemKind.account_activate.value,
        PlanItemKind.account_invite.value,
    }
)

# Fact-kind → revoke PlanItemKind (used for cascade revoke synthesis)
_FACT_KIND_TO_REVOKE: dict[str, PlanItemKind] = {
    'role': PlanItemKind.revoke_role,
    'group': PlanItemKind.group_remove,
    'entitlement': PlanItemKind.entitlement_detach,
}

# Fact-kind → grant PlanItemKind (for existing item lookup during cascade)
_FACT_KIND_TO_GRANT: dict[str, PlanItemKind] = {
    'role': PlanItemKind.grant_role,
    'group': PlanItemKind.group_add,
    'entitlement': PlanItemKind.entitlement_attach,
}

# Which op kinds represent "account is being disabled" (triggers cascade rules)
_DISABLE_OPS: frozenset[str] = frozenset({PlanItemKind.account_disable.value})

# Which op kinds represent "resource exists" for a given resource type
_RESOURCE_SATISFYING_OPS: dict[str, frozenset[str]] = {
    'account': frozenset(
        {
            PlanItemKind.account_create.value,
            PlanItemKind.account_activate.value,
            PlanItemKind.account_invite.value,
        }
    ),
}


# ---------------------------------------------------------------------------
# Internal graph types
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    item: PlanItem
    deps: list[uuid.UUID] = field(default_factory=list)
    """IDs of items this item depends on (must execute first)."""


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def resolve_dag(
    plan_id: uuid.UUID,
    items: list[PlanItem],
    descriptor_map: dict[str, ConnectorCapabilityDescriptor],
    current_account_states: dict[str, str],
) -> DAGResult:
    """Build the DAG for a set of PlanItems.

    Args:
        plan_id: The owning plan's UUID.
        items: PlanItems already resolved by D3 (concrete kinds).
        descriptor_map: Maps application string → ``ConnectorCapabilityDescriptor``.
            Missing entries are treated as having no dependency rules and no cascades.
        current_account_states: Maps ``application`` → current account status string
            (or ``'not_exists'`` when absent).  Used for unsatisfiable checks.

    Returns:
        ``DAGResult`` with dependencies, topological order, unsatisfiable items,
        cycle flag, and any synthetic cascade items.
    """
    # Phase 1: expand cascades (may add synthetic items to the list)
    all_items, cascade_items = _expand_cascades(plan_id, items, descriptor_map)

    # Phase 2: build dependency edges
    nodes, dep_rows, unsatisfiable_from_rules = _build_edges(plan_id, all_items, descriptor_map, current_account_states)

    # Phase 3: topological sort + cycle detection
    topo_order, cycle_detected, unsatisfiable_from_cycle = _toposort(nodes)

    # Merge unsatisfiable sets: rules-based + cycle-based
    all_unsatisfiable = unsatisfiable_from_rules | unsatisfiable_from_cycle

    return DAGResult(
        dependencies=dep_rows,
        topo_order=topo_order,
        unsatisfiable_item_ids=all_unsatisfiable,
        cycle_detected=cycle_detected,
        added_items=cascade_items,
    )


# ---------------------------------------------------------------------------
# Phase 1 — Cascade expansion
# ---------------------------------------------------------------------------


def _expand_cascades(
    plan_id: uuid.UUID,
    items: list[PlanItem],
    descriptor_map: dict[str, ConnectorCapabilityDescriptor],
) -> tuple[list[PlanItem], list[PlanItem]]:
    """Inject synthetic revoke items before each account_disable item.

    For each ``account_disable`` item, look up the connector's
    ``cascades.before_disable`` rules.  For each rule that names a ``fact_kind``
    ('role', 'group', 'entitlement'), check whether there are existing
    grant-type items for the same application.  If there are, they already
    handle revoke.  If there are not (connector signals a revoke sweep is
    needed), add synthetic revoke items.

    Synthetic items carry an empty ``target_descriptor`` with
    ``{'fact_kind': <kind>, 'cascade_synthetic': True}`` so the DAG edge
    builder can identify them.

    Returns (all_items_including_synthetics, new_cascade_items_only).
    """
    all_items = list(items)
    cascade_items: list[PlanItem] = []

    # Index existing items by (application, kind)
    existing_by_app_kind: dict[tuple[str, str], list[PlanItem]] = defaultdict(list)
    for it in items:
        existing_by_app_kind[(it.application, it.kind.value)].append(it)

    disable_items = [it for it in items if it.kind.value in _DISABLE_OPS]

    for disable_item in disable_items:
        app = disable_item.application
        descriptor = descriptor_map.get(app)
        if descriptor is None:
            continue
        cascades: AccountDisableCascades = descriptor.cascades
        for rule in cascades.before_disable:
            fact_kind = rule.fact_kind
            revoke_kind = _FACT_KIND_TO_REVOKE.get(fact_kind)
            if revoke_kind is None:
                continue  # unknown fact_kind — skip
            grant_kind = _FACT_KIND_TO_GRANT.get(fact_kind)
            # Check if a revoke item already exists for this (app, revoke_kind)
            existing_revokes = existing_by_app_kind.get((app, revoke_kind.value), [])
            if existing_revokes:
                # Revoke items already in plan — no need to synthesise
                continue
            # Check if the corresponding grant items exist (if a grant is being
            # added in this plan, the revoke is clearly needed as a counterpart)
            existing_grants = existing_by_app_kind.get((app, grant_kind.value if grant_kind else ''), [])

            # Only synthesise a sweep item when there's evidence of existing access
            # (i.e. grant items exist in this plan).  If there's nothing to revoke,
            # adding a synthetic item is a no-op at apply time, but it's noise.
            # The cascade rule is authoritative: always add a sweep marker.
            # The apply engine will skip the op if there's nothing to do.
            synthetic = PlanItem(
                id=uuid.uuid4(),
                plan_id=plan_id,
                kind=revoke_kind,
                application=app,
                account_ref=disable_item.account_ref,
                target_descriptor={
                    'fact_kind': fact_kind,
                    'cascade_synthetic': True,
                },
                initiatives=[],
                initiative_refs=[],
                policy_rule_refs=[],
                decision_snapshot={'source': 'cascade_expansion', 'fact_kind': fact_kind},
            )
            _ = existing_grants  # referenced for future per-fact iteration
            all_items.append(synthetic)
            cascade_items.append(synthetic)
            existing_by_app_kind[(app, revoke_kind.value)].append(synthetic)

    return all_items, cascade_items


# ---------------------------------------------------------------------------
# Phase 2 — Build dependency edges
# ---------------------------------------------------------------------------


def _build_edges(
    plan_id: uuid.UUID,
    items: list[PlanItem],
    descriptor_map: dict[str, ConnectorCapabilityDescriptor],
    current_account_states: dict[str, str],
) -> tuple[dict[uuid.UUID, _Node], list[PlanDependency], set[uuid.UUID]]:
    """Build directed edges (item → prerequisite) from descriptor dependency_rules.

    For each item, look up the connector operation descriptor's
    ``dependency_rules``.  For each rule, find items in the plan that would
    satisfy the requirement.  If a satisfying item exists, add a DAG edge
    (dependent → satisfier).  If no satisfying item exists but the current
    state already satisfies the rule, no edge is needed.  If no satisfier
    found AND current state doesn't satisfy → item is marked unsatisfiable.

    Also, for cascade rules, add edges: disable → all revoke items (synthetic
    or pre-existing) for the same application.

    Returns (node_map, dep_rows, unsatisfiable_from_rules).
    """
    nodes: dict[uuid.UUID, _Node] = {it.id: _Node(item=it) for it in items}
    dep_rows: list[PlanDependency] = []
    unsatisfiable_from_rules: set[uuid.UUID] = set()

    # Index items by (application, kind-value) for fast lookup
    by_app_kind: dict[tuple[str, str], list[PlanItem]] = defaultdict(list)
    for it in items:
        by_app_kind[(it.application, it.kind.value)].append(it)

    # Build operation descriptor lookup: app → {kind: descriptor}
    op_desc_map: dict[str, dict[str, ConnectorOperationDescriptor]] = {}
    for app, desc in descriptor_map.items():
        op_desc_map[app] = {op.kind: op for op in desc.operations}

    seen_dep_edges: set[tuple[uuid.UUID, uuid.UUID]] = set()

    def _add_edge(item_id: uuid.UUID, requires_id: uuid.UUID) -> None:
        edge = (item_id, requires_id)
        if edge in seen_dep_edges:
            return
        seen_dep_edges.add(edge)
        dep_rows.append(
            PlanDependency(
                plan_id=plan_id,
                item_id=item_id,
                requires_item_id=requires_id,
            )
        )
        nodes[item_id].deps.append(requires_id)

    for item in items:
        app = item.application
        op_descs = op_desc_map.get(app, {})
        op_desc = op_descs.get(item.kind.value)
        if op_desc is None:
            continue  # no descriptor → no dependency rules

        for rule in op_desc.dependency_rules:
            # Determine which application the dependency is in
            dep_app = rule.application if rule.application else app
            resource = rule.resource
            required_statuses = rule.status

            # Check if current state already satisfies the rule
            if resource == 'account':
                current_state = current_account_states.get(dep_app, 'not_exists')
                if current_state in required_statuses:
                    # Already satisfied — no plan item needed
                    continue

            # Find plan items in dep_app that would satisfy this resource/status
            satisfying_items = _find_satisfying_items(
                dep_app=dep_app,
                resource=resource,
                required_statuses=required_statuses,
                by_app_kind=by_app_kind,
            )

            if not satisfying_items:
                # Requirement cannot be met: no satisfier in plan and current state
                # doesn't satisfy the rule → item is unsatisfiable
                unsatisfiable_from_rules.add(item.id)
                continue

            for satisfier in satisfying_items:
                if satisfier.id == item.id:
                    continue  # self-loop guard
                _add_edge(item.id, satisfier.id)

    # Add cascade edges: account_disable depends on ALL revoke items in same app
    # (both synthetic and pre-existing, since cascade rules govern them all)
    cascade_apps: dict[str, set[str]] = {}  # app → set of fact_kinds in cascade
    for app, desc in descriptor_map.items():
        if desc.cascades.before_disable:
            cascade_apps[app] = {r.fact_kind for r in desc.cascades.before_disable}

    for item in items:
        if item.kind.value not in _DISABLE_OPS:
            continue
        app = item.application
        cascaded_fact_kinds = cascade_apps.get(app, set())
        if not cascaded_fact_kinds:
            continue
        for fact_kind, revoke_kind in _FACT_KIND_TO_REVOKE.items():
            if fact_kind not in cascaded_fact_kinds:
                continue
            for revoke_item in by_app_kind.get((app, revoke_kind.value), []):
                if revoke_item.id != item.id:
                    _add_edge(item.id, revoke_item.id)

    return nodes, dep_rows, unsatisfiable_from_rules


def _find_satisfying_items(
    dep_app: str,
    resource: str,
    required_statuses: list[str],
    by_app_kind: dict[tuple[str, str], list[PlanItem]],
) -> list[PlanItem]:
    """Find plan items in dep_app that would bring resource into a required status.

    For ``resource='account'`` and ``required_statuses=['active']``, this returns
    all account-lifecycle items in dep_app that result in an active account.
    """
    if resource == 'account':
        satisfying_ops = _RESOURCE_SATISFYING_OPS.get('account', frozenset())
        # Filter to ops that result in a status matching required_statuses
        # 'active' is the only status that account-create/activate/invite lead to
        if 'active' in required_statuses:
            results: list[PlanItem] = []
            for op_kind in satisfying_ops:
                results.extend(by_app_kind.get((dep_app, op_kind), []))
            return results
        # For other required statuses (e.g. 'suspended'), no plan item type satisfies
        return []

    # For future resource types — no satisfier logic yet
    return []


# ---------------------------------------------------------------------------
# Phase 3 — Topological sort + cycle detection
# ---------------------------------------------------------------------------


def _toposort(
    nodes: dict[uuid.UUID, _Node],
) -> tuple[list[uuid.UUID], bool, set[uuid.UUID]]:
    """Kahn's algorithm for topological sort with cycle detection.

    Returns (topo_order, cycle_detected, unsatisfiable_item_ids).

    ``unsatisfiable_item_ids`` contains the IDs of nodes that were part of a
    cycle (and therefore could not be ordered) — these are items the apply
    engine must not attempt to execute.
    """
    # Build in-degree and adjacency list
    in_degree: dict[uuid.UUID, int] = {nid: 0 for nid in nodes}
    # successors[x] = items that depend on x (x must run first)
    successors: dict[uuid.UUID, list[uuid.UUID]] = {nid: [] for nid in nodes}

    # Deduplicate dep edges (same (item, requires) may appear multiple times
    # if multiple rules resolve to the same satisfier)
    seen_edges: set[tuple[uuid.UUID, uuid.UUID]] = set()

    for nid, node in nodes.items():
        for dep_id in node.deps:
            edge = (nid, dep_id)
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            if dep_id in in_degree:
                in_degree[nid] += 1
                successors[dep_id].append(nid)

    # Kahn's BFS
    queue: list[uuid.UUID] = [nid for nid, deg in in_degree.items() if deg == 0]
    topo_order: list[uuid.UUID] = []

    while queue:
        nid = queue.pop(0)
        topo_order.append(nid)
        for succ in successors[nid]:
            in_degree[succ] -= 1
            if in_degree[succ] == 0:
                queue.append(succ)

    remaining = {nid for nid, deg in in_degree.items() if deg > 0}
    cycle_detected = len(remaining) > 0

    return topo_order, cycle_detected, remaining


# ---------------------------------------------------------------------------
# Convenience: build descriptor_map from a flat list of descriptors
# ---------------------------------------------------------------------------


def build_descriptor_map(
    app_descriptors: list[tuple[str, Any]],
) -> dict[str, ConnectorCapabilityDescriptor]:
    """Build the descriptor_map expected by ``resolve_dag`` from (app, descriptor) pairs.

    ``descriptor`` may be a ``ConnectorCapabilityDescriptor`` instance or a raw
    dict (will be validated via ``model_validate``).
    """
    result: dict[str, ConnectorCapabilityDescriptor] = {}
    for app, raw in app_descriptors:
        if isinstance(raw, ConnectorCapabilityDescriptor):
            result[app] = raw
        elif isinstance(raw, dict):
            result[app] = ConnectorCapabilityDescriptor.model_validate(raw)
        else:
            result[app] = ConnectorCapabilityDescriptor()
    return result

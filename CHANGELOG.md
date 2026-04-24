# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `POST /sod-rules/apply` — idempotent config-as-code upsert for SoD rules; capabilities referenced by slug; full condition sync (create/replace/delete) in one transaction; returns diff summary (`rules_created`, `rules_updated`, `rules_unchanged`, `conditions_created`, `conditions_deleted`); 422 on unknown capability slugs
- `apply_service.py` in `sod_rules/` slice — pure upsert logic keyed by rule `code` and condition `name`
- `SodApplyPayload`, `SodConditionSpec`, `SodRuleSpec`, `SodApplyResult` schemas in `sod_rules/schemas.py`
- Development seed script `scripts/seed_dev.py` — Meridian Fintech scenario (2 apps, 3 employees, 1 NHI, 6 accounts, 5 capabilities, 5 mappings, 2 SoD rules, 6 grants)

### Fixed

- `POST /scan-runs`, `PATCH /scan-runs/{id}/status`, `POST /scan-runs/{id}/run` routes missing `await session.commit()` — scan run rows were created in-memory but never persisted to the database
- `GET /capability-grants` mandatory-filter requirement removed; without filters the response is capped at 100 rows to prevent full-table scans; filtered queries use the requested `limit`

### Added

- Phase 13 SoD & Access Analysis complete (19/19 milestones)
- End-to-end integration test covering capability projection, scan, mitigation, feedback, and on-demand SoD evaluate / what-if

- Phase 13 Step 17 — CLI coverage for the access-analysis REST surface: seven new `al` commands across four plugins (`al sod evaluate`, `al sod what-if`, `al sod resolve-capabilities`, `al scan run`, `al scan list`, `al findings list`, `al feedback post`). No server changes. See `aurelion-cli/CHANGELOG.md` for full command signatures.

- Endpoint `POST /sod/what-if` for read-only SoD evaluation with synthetic capability overrides (`CapabilityGrantOverride`); never persists, never emits events; `at` defaults to now(UTC) at route boundary; five server-side validations (`WhatIfCapabilityNotFoundError`, `WhatIfScopeKeyNotFoundError`, `WhatIfApplicationNotFoundError`, `WhatIfScopeValueMismatchError`, `WhatIfScopeValueInvalidError`) each mapped to HTTP 422
- `evaluators/exceptions.py` — new module with `WhatIfValidationError` base and five concrete what-if error types
- Three new evaluator repository helpers: `load_capability_id_and_slug`, `scope_key_exists`, `application_exists` (one-shot SELECT each, no joins)
- `SodEvaluatorService.what_if_subject` — read-only method loading DB grants + mitigations + rules, converting `CapabilityGrantOverride` list to synthetic `CapabilityGrantView` objects via `_override_to_view` (negative sentinel IDs, zero-UUID source grant), and calling the pure evaluator with `capability_overrides`
- `FindingMitigationLinkageMissingError` and `FindingMitigationNotApplicableError` exceptions in `findings/exceptions.py`
- `get_mitigation_for_linkage` cross-slice repository helper in `findings/repository.py` (reads `mitigations` table directly, no import of MitigationService)
- Feedback slice for SoD findings, capability mappings, and rules; emits `feedback.posted` on `aurelion.events` — immutable append-only audit trail with `FeedbackKind` enum (`accepted_risk`, `false_positive`, `needs_mapping_fix`, `needs_rule_fix`, `needs_mitigation`); `POST /feedbacks`, `GET /feedbacks`, `GET /feedbacks/{id}`; no PATCH/DELETE
- `feedback_kind` PG enum owned by the feedbacks slice; `feedbacks` table with FK RESTRICT to `sod_rules`, `capability_mappings`, `findings`, `subjects`; CHECK constraint enforcing at least one target FK set
- ScanEngine orchestrator with bulk-loaded SoD inputs, deduplicated findings, and mitigation relinking
- `POST /scan-runs/{id}/run` synchronous endpoint
- `ScanRun` columns `findings_created_count` and `findings_reused_count`
- Five scan/finding event types with strict `correlation_id` / `causation_id` chain
- Phase 13 Step 13: unused access detector — pure `detect_unused(access_facts, threshold_days, at)` function in `detectors/unused.py`; default severity `low`; default threshold 90 days; emits one `UnusedFinding` per `AccessFact` (not per `EffectiveGrant`); no persistence, no events, no `at` API parameter
- `UnusedDetectorService` with LEFT JOIN usage aggregate subquery + INNER JOIN resources for `application_id`; bounded single SELECT; never flushes, never emits
- `POST /access-analysis/detect-unused` read-only endpoint; `threshold_days` range 1–3650 (default 90); `limit` range 1–5000 (default 1000); optional `application_id` filter
- `AccessFactView` frozen strict Pydantic DTO and `UnusedFinding` frozen dataclass; `DetectUnusedRequest` / `UnusedFindingResponse` schemas
- `get_unused_detector_service` dependency in `detectors/deps.py`
- Terminated-subject access detector with `detect_terminated` pure function and `DEFAULT_TERMINATED_SEVERITY` constant
- `TerminatedDetectorService` with INNER JOIN subjects and `TERMINAL_STATUSES_BY_KIND` vocabulary
- `POST /access-analysis/detect-terminated` read-only endpoint; no persistence, no events
- Orphan access detector with `detect_orphans` pure function and `DEFAULT_ORPHAN_SEVERITY` constant
- `OrphanDetectorService` loading unlinked accounts via LEFT JOIN ownership assignments (no N+1)
- `POST /access-analysis/detect-orphans` read-only endpoint; no persistence, no events
- Pure SoD Evaluator (`evaluate`) in `src/capabilities/access_analysis/evaluators/sod.py` — deterministic, IO-free, DB-free; accepts `capability_overrides` for what-if analysis; evidence-hash contract frozen (SHA-256 over stable IDs: subject_id, access_fact_ids, initiative_ids, capability_mapping_ids, rule_id, scope_key_id, scope_value — EffectiveGrant.id deliberately excluded)
- `Violation` frozen dataclass — 15 fields including `matched_capability_slugs` (sorted), `matched_effective_grant_ids` (list[UUID]), `is_mitigated`, `active_mitigation_id`, `proposed_mitigation_id`, `evidence_hash`, `evaluated_at`
- Specific-overrides-generic mitigation resolution: exact-scope tier wins over generic (None/None); within tier active > proposed > most recent `created_at`; only `active` status flips `is_mitigated=True`
- `POST /sod/evaluate` read-only endpoint — returns `list[SodViolationResponse]`; default `at=now(UTC)` resolved at route boundary; empty list for no-capabilities or nonexistent subject; emits no events; inserts no rows
- `SodEvaluatorService` — reads SodRules + CapabilityGrants + Mitigations in two queries each (no N+1); passes to pure evaluator; never calls `session.flush()` or `session.commit()`
- `SodEvaluateRequest` (extra='forbid'), `SodViolationResponse` schemas
- `sod_evaluator_router` registered in `src/routers/v0.py` at `/sod/evaluate` (coexists with existing `/sod/resolve-capabilities`)
- 31 tests across four files: pure-function matrix (18 cases), evidence-hash stability (5 cases), service-layer integration (3 DB-backed cases), HTTP route (5 cases)
- `Mitigation` slice (`models.py`, `schemas.py`, `repository.py`, `service.py`, `routes.py`, `deps.py`, `exceptions.py`, `tests/`) — per-subject, per-rule, time-bound mitigation records with owner, status lifecycle (`proposed → active → revoked`; `expired` reserved for future sweep), and PROTECT FK into `mitigation_controls`
- `mitigation_status` Postgres enum (`proposed`, `active`, `expired`, `revoked`) owned by this slice
- Four lifecycle domain events on `aurelion.events`: `access_analysis.mitigation.created`, `access_analysis.mitigation.activated`, `access_analysis.mitigation.revoked`, `access_analysis.mitigation.expired` (event contract reserved; sweep scheduler out of scope)
- Partial unique index `uq_mitigations_active_or_proposed` with `NULLS NOT DISTINCT` (PG17) on `(rule_id, subject_id, scope_key_id, scope_value) WHERE status IN ('active', 'proposed')`
- Deferred FK constraints from `findings.active_mitigation_id` and `findings.proposed_mitigation_id` → `mitigations.id` (columns were plain BigInteger in Step 7; FK closure happens here)
- `mitigation_allowed=false` enforcement: service rejects creation when the referenced `SodRule` has `mitigation_allowed=false`
- Six REST endpoints: `POST /mitigations`, `GET /mitigations`, `GET /mitigations/{id}`, `POST /mitigations/{id}/activate`, `POST /mitigations/{id}/revoke`, `PATCH /mitigations/{id}/status`
- Alembic migration `ops/db_versions/2026_04_24_1400_phase_13_step_09_mitigations.py` (revision `b2c3d4e5f6a8`, down_revision `a1b2c3d4e5f7`)
- `MitigationControl` catalog slice with soft-delete, immutable code, and seeded default control types; 5 CRUD endpoints
- `ScanRun` storage slice with status-transition guards (`pending→running→completed|failed`); `started_at`/`completed_at` set on transition; endpoints: `POST /scan-runs`, `GET /scan-runs`, `GET /scan-runs/{id}`, `PATCH /scan-runs/{id}/status`
- `Finding` storage slice with status-transition guards (`open→acknowledged|mitigated`, `acknowledged→mitigated`, any non-terminal→`resolved` with required reason); no `FindingCreate` API (engine writes in Step 14); endpoints: `GET /findings`, `GET /findings/{id}`, `PATCH /findings/{id}/status`
- `scan_run_status` Postgres enum (`pending`, `running`, `completed`, `failed`), `scan_run_trigger` Postgres enum (`manual`, `api`, `schedule`), `finding_kind` Postgres enum (`sod`, `orphan_access`, `terminated_access`, `unused_access`), `finding_status` Postgres enum (`open`, `acknowledged`, `resolved`, `mitigated`) — all four owned by this step; `sod_severity` reused via `create_type=False`
- Alembic migration `ops/db_versions/2026_04_24_1200_phase_13_step_07_scan_runs_findings.py` (revision `f0a1b2c3d4e5`, down_revision `e8f9a0b1c2d3`) covering all four new enums, `scan_runs` table (3 CHECK constraints, 4 indexes, 2 UUID FKs), `findings` table (3 CHECK constraints, 1 UNIQUE constraint, 7 indexes, 5 FKs); `active_mitigation_id`/`proposed_mitigation_id` plain BigInteger (FK added by Step 9)
- `SodRule` CRUD slice with immutable code, scope-mode invariants (service + Postgres CHECK), soft-delete via `POST /sod-rules/{id}/deactivate`; endpoints: `POST /sod-rules`, `GET /sod-rules`, `GET /sod-rules/{id}`, `PATCH /sod-rules/{id}`, `POST /sod-rules/{id}/deactivate`
- `SodRuleCondition` slice with `sod_rule_condition_capabilities` M2M association; conditions immutable (DELETE + POST to replace); capability_ids resolved via explicit SQL; endpoints: `POST /sod-rules/{rule_id}/conditions`, `GET /sod-rules/{rule_id}/conditions`, `GET /sod-rules/{rule_id}/conditions/{id}`, `DELETE /sod-rules/{rule_id}/conditions/{id}`
- `sod_severity` Postgres enum (`critical`, `high`, `medium`, `low`, `informational`) and `sod_rule_scope` Postgres enum (`global`, `per_application`, `by_scope_key`) — both owned by this step; downstream slices must use `create_type=False`
- Alembic migration `ops/db_versions/2026_04_24_1100_phase_13_step_06_sod_rules.py` (revision `e8f9a0b1c2d3`) covering both enums, both tables, association table, all CHECK constraints, all indexes, all FKs
- `CapabilityResolverService` with read-only `resolve_capabilities_for_sources` pre-flight slug resolver
- `POST /api/v0/sod/resolve-capabilities` endpoint returning distinct sorted capability slugs, never persists
- `load_active_mappings` extracted to `mapping_loader.py`; `matcher_applies` made public
- `CapabilityGrant` projection table with read-only API (`GET /capability-grants`, `GET /capability-grants/{id}`)
- Pure `capability_projector` with resource_id / resource_kind / resource_path_glob matching and four-kind scope_value source (subject_attribute, resource_attribute, application_id, constant)
- `CapabilityProjectionService` writer with PG `ON CONFLICT` upsert; `application_id` immutable post-projection
- `CapabilityGrantReadService` with mandatory-filter guard on list endpoint
- `_count_dependent_capability_grants` in capability_mappings wired to real `COUNT(*)` query
- `CapabilityMapping` CRUD slice with three-column XOR resource match (resource_id / resource_kind / resource_path_glob) and CHECK constraint
- Discriminated-union `scope_value_source` (subject_attribute / resource_attribute / application_id / constant) validated at API boundary
- `action_slug` validated against `ref_actions` vocabulary; FK violations translated to typed domain exceptions
- In-use cascade check stub for `CapabilityGrant` (activated in Step 4)
- `CapabilityScopeKey` vocabulary slice with immutable code, soft-delete, and default 17-code seed migration
- `Capability` vocabulary slice with immutable slug and soft-delete via `POST /capabilities/{id}/deactivate`
- Reconciliation handler registry (`capabilities/reconciliation/registry.py`) mapping `artifact_type → Handler`
- `NormalizationResult` frozen dataclass and `Handler` Protocol in `contracts.py`
- `pipeline.run_reconciliation` with per-application set-diff on normalized fact key (create / reactivate / update / revoke)
- `capabilities/reconciliation/handlers/` sub-package with smoke `role` handler
- `ensure_resource_by_identity` for handler-driven Resource auto-provisioning
- `AccessFactService.refresh_fact_fields` + `AccessFactNotActiveError` + `inventory.access_fact.updated` event
- `reconciliation.run.completed` event (three-segment routing key, WARNING when `facts_revoked > 100`)
- `POST /reconciliation/runs` endpoint returning eight-field run summary
- `al reconciliation run --application-id <UUID>` CLI command
- Phase 12 Universal Access Artifacts + Normalized Access Facts complete (15/15 milestones)
- `sap_role`, `acl_entry`, `db_grant`, `privilege` smoke handlers covering all four Phase 12 artifact classes
- DB-grant handler with `SELECT→read`, `INSERT/UPDATE/DELETE→write`, `EXECUTE→execute`, `ADMIN OPTION→admin` mapping; non-standard privileges silently dropped
- End-to-end integration tests seeding via `AccessArtifactService.upsert_artifact` and running via `ReconciliationService.run` across all five artifact types in a single pipeline run
- Re-run idempotency and tombstone-driven revocation end-to-end tests
- `test_handler_vocabulary_guard` — AST-walk static guard asserting all `action_slug` literals exist in seeded `ref_actions` vocabulary
- Repo-wide `ent_roles` / `ent_privileges` / `role_id` / `privilege_id` grep-guard across `aurelion-kernel/src/`

### Changed

- `PATCH /findings/{id}/status` now validates `active_mitigation_id` linkage when transitioning to `mitigated`: referenced Mitigation must exist, have `status=active`, have a valid time window covering now, match the finding's `(rule_id, subject_id)`, and match scope via specific-overrides-generic rule (exact scope or unscoped fallback)
- `FindingStatusPatch` schema gains optional `active_mitigation_id: int | None = None` field (backward-compatible; `extra='forbid'` preserved)
- `update_finding_status_fields` in `findings/repository.py` accepts optional `active_mitigation_id` parameter and stamps it on the row when transitioning to `mitigated`

- `ReconciliationService` rewritten around artifact-first `pipeline.run_reconciliation`; legacy role/privilege/account-centric orchestration removed
- `capabilities/reconciliation/handlers/__init__.py` imports four new handler modules (`acl_entry`, `db_grant`, `privilege`, `sap_role`) alongside the existing `role` — registration fires at kernel bootstrap for all five

### Removed

- Legacy reconciliation `engine.py`, `orchestrator.py`, and `reconciler_account.py`
- Role/privilege-specific DTOs from `schemas.py`
- Legacy `POST /applications/{id}/reconcile` connector-based reconciliation endpoint

### Changed (continued)

- `AccessFact` as current-state store: `action_id` FK to `ref_actions`, partial unique indexes on active rows, reactivate-on-re-grant idempotency, application-scope invariant guard, `revoke_fact` / `inventory.access_fact.revoked` + `inventory.access_fact.reactivated` events, `invalidate_fact` removed, EAS read-path patched to JOIN `ref_actions`
- `ArtifactBinding` redesigned as a generic polymorphic target binding: `(target_type, target_id)` pair replaces three nullable FK columns; UNIQUE `(artifact_id, target_type, target_id)` enforces dedup; `GET /artifact-bindings` query params updated to `?target_type=&target_id=`

### Added

- `AccessArtifactService.tombstone_artifact` with idempotent `inventory.access_artifact.tombstoned` domain event
- Reactivation-on-upsert: `upsert_access_artifact` restores `is_active=true`, `tombstoned_at=NULL` when a tombstoned row is re-observed
- `is_active` filter on `GET /access-artifacts`

### Changed

- `inventory.access_artifact.ingested` replaces `inventory.access_artifact.created`; payload extended with `raw_name`, `effect`, `valid_from`, `valid_until` (timestamps as ISO-8601)
- `AccessArtifact` create-path: `AccessArtifactService.create_artifact(...)` replaced by `upsert_artifact(...) -> tuple[AccessArtifact, bool]` using PG `INSERT ... ON CONFLICT DO UPDATE` on `uq_access_artifacts_application_id_artifact_type_external_id`. Re-observation refreshes `payload`, `observed_at`, `ingest_batch_id`, `ingested_at` in place; `is_active` and `tombstoned_at` are preserved across upserts (lifecycle transitions are Step 11's concern). Repository `create_access_artifact` replaced by `upsert_access_artifact` returning `(artifact, was_inserted)`. `was_inserted` derived from `RETURNING (xmax = 0)` — safe because `access_artifacts` is not partitioned (see ARCH_CONTEXT). `inventory.access_artifact.created` event now emits only on fresh inserts (`was_inserted=True`); update path is silent until Step 10. (Phase 12 Step 8)
- `AccessArtifact`: renamed `source_kind` column → `artifact_type` (DB column, ORM field, Pydantic schema field, `GET /access-artifacts?source_kind=` query param renamed to `?artifact_type=`, and `inventory.access_artifact.created` event payload key). **Breaking** for clients that relied on the old names. (Phase 12 Step 7)

### Added

- `AccessArtifact` permitted universal fields: `raw_name` (`String(255)`, nullable), `effect` (`Text`, nullable, source-raw string; NOT normalized to `allow|deny` — the normalized `allow|deny` contract lives on `AccessFact.effect` (Step 13)), `valid_from` (`TIMESTAMPTZ`, nullable), `valid_until` (`TIMESTAMPTZ`, nullable). All four nullable, no defaults, no CHECK constraints, no indexes. Added to ORM model, `AccessArtifactCreate` / `AccessArtifactRead` schemas, `upsert_artifact` / `upsert_access_artifact` signatures, and the upsert `set_` dict (re-observation refreshes these fields in place, same semantics as `payload`; passing `None` sets field to `NULL`). Event payload is unchanged — `inventory.access_artifact.created` still carries the Step 8 shape; Step 10 will extend payload with these four fields. Alembic migration `2026_04_24_0300_add_access_artifact_permitted_universal_fields.py` (revision `c4d5e6f7a8b9`) adds the four columns as nullable. (Phase 12 Step 9)
- `AccessArtifact` lifecycle columns: `observed_at` (TIMESTAMPTZ NOT NULL, defaults to `now()` on create when not provided by caller), `is_active` (BOOLEAN NOT NULL DEFAULT TRUE), `tombstoned_at` (TIMESTAMPTZ NULL). UNIQUE constraint `uq_access_artifacts_application_id_artifact_type_external_id` on `(application_id, artifact_type, external_id)`. `DuplicateAccessArtifactError` raised on identity-triple collision, mapped to HTTP 409 on the create path. Migration: `2026_04_24_0200_access_artifact_artifact_type_and_lifecycle`. (Phase 12 Step 7)
- `Resource`: normalized identity columns `resource_type` (VARCHAR 255) and `resource_key` (VARCHAR 1024) with UNIQUE constraint `uq_resources_application_id_resource_type_resource_key`. Additive to existing `kind` / `external_id`. Transitional defaults: when not provided on create, `resource_type = kind`, `resource_key = external_id`. New service method `get_resource_by_identity(application_id, resource_type, resource_key)` for internal lookup. Migration: `2026_04_24_0100_add_resource_identity_columns`. (Phase 12 Step 6)
- Reference table `ref_actions` with seeded minimum vocabulary (`read`, `write`, `execute`, `approve`, `admin`, `use`, `own`) via migration `2026_04_24_0000_add_ref_actions`. Backing ORM: `src/inventory/actions/models.py` (`Action` class). Pydantic schemas, service, REST endpoints, and CLI commands are deferred to subsequent Phase 12 Step 2 sub-steps.
- `ActionRead` Pydantic v2 schema (`src/inventory/actions/schemas.py`) and read-only `ActionService` (`src/inventory/actions/service.py`) with `list_actions()` and `get_action_by_slug(slug)` methods. Service takes `AsyncSession` + `LogService`; emits no domain events (reference vocabulary has no domain lifecycle). REST endpoints (Step 4) and CLI commands (Step 5) follow.
- `GET /actions` and `GET /actions/{slug}` read-only REST endpoints for the `Action` reference vocabulary; `404` on unknown slug; case-sensitive lookup; no mutation endpoints

### Removed

- `DuplicateAccessArtifactError` and `_translate_access_artifact_create_integrity_error` from `inventory/access_artifacts/service.py` — both unreachable after upsert wiring. Callers that caught `DuplicateAccessArtifactError` must be updated. (Phase 12 Step 8)
- `Role` and `Privilege` inventory slices (ORM models, schemas, repositories, reconcilers, tables `ent_roles` / `ent_privileges`)
- Reconciliation orchestrator trimmed to accounts-only; `roles` / `privileges` branches and result fields gone
- `ReconciliationResult` no longer carries `roles` / `privileges` fields; `reconciliation.completed` log event drops the four role/privilege counters
- CLI sweep confirmed clean — no role/privilege commands existed

## [0.1.3] - 2026-04-22

### Added

- **Engineering Studio — Events & Logs panel (Phase 11 Step 5).** New read endpoints `GET /api/v0/platform/events` and `GET /api/v0/platform/logs`. Events are buffered in-process via `InMemoryEventBuffer` (capped `deque(maxlen=500)`) behind a `TeeEventSink` — RabbitMQ publish stays primary, the buffer is a best-effort tap (errors in the tap never affect the primary). Logs are served from the existing `log_event_buffer` without the discriminator-filter requirement of `/api/v0/log-buffer`.

### Changed

- Introduced `src/core/http/errors.py::translate_service_errors` — a minimal context manager that maps slice-specific service errors to `HTTPException` via an explicit per-call table. Applied to `inventory/resources`, `inventory/subjects`, and `inventory/nhi` route files as proof of concept (~25 repetitive `except` blocks collapsed). No HTTP contract change; all existing route tests pass. Rule documented in `ARCH_CONTEXT.md`.
- Refactored `SubjectService` and `ResourceService` to the "services orchestrate only" pattern: extracted inline validators, inline `EventEnvelope` assembly, and inline `IntegrityError` translation into named helper functions within each slice. No public signatures changed, no event payload changes. Rule documented in `ARCH_CONTEXT.md`.
- Simplified LogService plumbing: collapsed pure-delegation layers, unified fire-and-forget wrapper (`_run_fire_and_forget`), extracted `_resolve_sink` helper, documented the four-way app-log / domain-event / audit-record / trace-metadata split. Public signatures unchanged (40+ call sites untouched).
- RabbitMQ configuration centralized in `Settings` (composition root): 8 connection/exchange fields + `rabbitmq_url` property
- `AsyncRabbitMQPublisher` `url` argument keyword-only and required; no env fallback
- `RabbitMQEventSink` and `RabbitMQLogSink` require `exchange` keyword argument; composition root injects value
- `run_connector_registration_consumer` fully argument-driven; no internal `os.environ` reads
- Operators must migrate `AURELION_RABBITMQ_*` / `AURELION_*_EXCHANGE` env keys to unprefixed `RABBITMQ_*` forms — see `.env.example`
- Unset `RABBITMQ_USERNAME`/`RABBITMQ_PASSWORD` now resolve to `'guest'` via Settings defaults (was Python `None`)

## [0.1.2] - 2026-04-21

### Added

- Phase 10 Events ↔ Logs Decoupling complete (23/23 milestones)
- `AsyncRabbitMQPublisher` with persistent `RobustConnection`, publisher confirms, and exponential back-off retry
- `AsyncRabbitMQRPCClient` with `asyncio.Future`-based reply dispatch and exclusive per-client reply queue
- `CapturingLogSink` test double in `src/platform/logs/testing.py`
- Two-bus separation canary suite (`test_canary_two_bus_separation.py`, 4 structural assertions)
- Two-bus invariant added to `ARCH_CONTEXT.md` and `EVENT_MODEL_GUIDELINES.md`
- `platform/events/` slice: `EventEnvelope` schema, `EventSink` protocol, `EventService`, `NoOpEventService`, `RabbitMQEventSink`, `EventSinkFactory`, `CapturingEventService`
- EAS consumer (`mq_eas_projection_consumer`) rebound from `aurelion.logs` to `aurelion.events`
- `capabilities/effective_access` producer migration (DROP variant) — `eas.projection.completed`, `eas.projection.failed`
- `inventory/secrets` producer migration (KEEP variant) — `inventory.secret.created`, `inventory.secret.deleted`
- `inventory/initiatives` producer migration (DROP variant) — `inventory.initiative.created`, `inventory.initiative.updated`, `inventory.initiative.expired`
- `inventory/customers` producer migration (DROP variant) — `inventory.customer.created`, `inventory.customer.updated`, `inventory.customer.attribute_added`, `inventory.customer.attribute_removed`
- `inventory/resources` producer migration (DROP variant) — `inventory.resource.created`, `inventory.resource.updated`, `inventory.resource.attribute_added`, `inventory.resource.attribute_removed`
- `inventory/employee_records` producer migration (DROP variant) — `inventory.employee_record.created`, `inventory.employee_record.attribute_added`, `inventory.employee_record.attribute_removed`
- `inventory/nhi` producer migration (DROP variant) — `inventory.nhi.created`, `inventory.nhi.attribute_added`, `inventory.nhi.attribute_removed`
- `inventory/subjects` producer migration (DROP variant) — `inventory.subject.created`, `inventory.subject.updated`, `inventory.subject.attribute_added`, `inventory.subject.attribute_removed`, `inventory.subject.status_changed`
- `inventory/ownership_assignments` producer migration (DROP variant) — `inventory.ownership_assignment.created`, `inventory.ownership_assignment.deleted`
- `inventory/persons` producer migration (DROP variant) — `inventory.person.created`, `inventory.person.attribute_added`, `inventory.person.attribute_removed`
- `inventory/employees` producer migration (DROP variant) — `inventory.employee.created`, `inventory.employee.attribute_added`, `inventory.employee.attribute_removed`
- `inventory/lake_batches` producer migration (KEEP variant) — `inventory.lake_batch.created`, `inventory.lake_batch.deleted`
- `inventory/threat_facts` producer migration (DROP variant) — `inventory.threat_fact.created`, `inventory.threat_fact.updated`
- `inventory/accounts` producer migration (DROP variant) — `inventory.account.updated`
- `inventory/artifact_bindings` producer migration (DROP variant) — `inventory.artifact_binding.created`
- `inventory/access_artifacts` producer migration (DROP variant) — `inventory.access_artifact.created`
- `inventory/access_usage_facts` producer migration (DROP variant)
- `inventory/access_facts` pilot producer migration — `inventory.access_fact.created`, `inventory.access_fact.invalidated`
- Session-scoped autouse fixture defaulting events provider to `noop` in test suite

### Changed

- **BREAKING:** `EventSink.emit` and `LogSink.emit` interfaces converted to `async def`; all sink implementations and callers updated
- **BREAKING:** `LogService.emit_log` and `LogService.emit_safe` no longer accept an `event_type` parameter; `NoOpLogService.emit_safe` likewise; domain routing must go through `EventService.emit(EventEnvelope(...))` on `aurelion.events`
- **BREAKING:** EAS runtime drops `AURELION_LOGS_EXCHANGE`; `AURELION_EVENTS_EXCHANGE` required instead
- EAS routing keys migrated to 3-segment canonical forms; `access_fact.updated` dead entry dropped
- EAS message dispatch switched to routing-key-based filtering with routing-key/envelope mismatch guard
- EAS deserialization switched from `LogEvent` to `EventEnvelope`; `normalize_mq_log_event_payload` removed
- `EventService.emit`, `CapturingEventService.emit` converted to `async def`
- `LogService.emit_log` converted to `async def`; `emit_safe` / `emit_event_safe` remain synchronous fire-and-forget
- `RabbitMQEventSink` and `RabbitMQLogSink` accept a shared `AsyncRabbitMQPublisher` injected in lifespan
- FastAPI lifespan manages shared `AsyncRabbitMQPublisher` and `AsyncRabbitMQRPCClient` lifecycle
- `ConnectorClient` accepts shared `AsyncRabbitMQRPCClient`
- `SecretService.create_secret` and `delete_secret` converted to `async def`
- `EffectiveAccessWriteService._project_fact`, `_project_pair`, `_emit_completed` converted to `async def`
- `capabilities/effective_access` `_COMPONENT` renamed `'effective_access'` → `'capabilities.effective_access'`
- `inventory/employees` `_COMPONENT` renamed `'identity-core'` → `'inventory.employees'`
- `inventory/persons` `_COMPONENT` renamed `'identity-core'` → `'inventory.persons'`
- `inventory/nhi` `_COMPONENT` renamed `'identity-core'` → `'inventory.nhi'`
- `inventory/secrets` `_COMPONENT` added as `'inventory.secrets'` (replaces inline `'secret-manager'` literals)
- Kwarg-sweep of 16 legacy call sites across capabilities and platform slices — mechanical argument-shape refactor, no bus change; slices remain on `aurelion.logs`
- DoD amendment: `LogEvent.event_type` field retained as deprecated legacy atavism; `LogService` public API has no `event_type` parameter; full field removal deferred to a dedicated later phase

### Removed

- `LogService.log_info`, `log_warning`, `log_error` convenience methods (zero production callers)

## [0.1.1] - 2026-04-19

### Added

- EAS end-to-end pipeline regression test (ACL ingest → Phase 08 normalization → incremental consumer → read API, four-wave fixture). No runtime change.

### Fixed

- Set-difference tombstoning on upsert-reprojection via `tombstone_effective_grants_for_missing_pairs`, closing silent-shrink gap when an initiative disappears without firing `initiative.expired`
- Per-initiative tombstoning: `initiative.expired` now tombstones only grants of the expired initiative (by `source_initiative_id`), not all grants of the associated access fact

### Changed

- EAS incremental apply API renamed: `apply_access_fact_change` → `apply_incremental_change`, `AccessFactChangeKind` → `IncrementalApplyKind` with new `INVALIDATE_INITIATIVE` value
- `ProjectionScopeKind.INITIATIVE` added to EAS schemas
- `mq_eas_projection_consumer` handler split into `_EVENT_TYPES_INVALIDATE_FACT` and `_EVENT_TYPES_INVALIDATE_INITIATIVE` routing tables; new `missing_initiative_id` operational log

### Added

- EAS incremental projection: `EffectiveAccessProjectionService.apply_incremental_change` (observed_at CAS upsert + per-initiative tombstone via `tombstone_effective_grants_for_initiative`) and `mq_eas_projection_consumer` runtime binding on `aurelion.logs` with routing keys `inventory.access_facts.*` and `inventory.initiatives.*`. Maps `access_fact.*` / `initiative.*` events to the apply API with ack-and-log delivery semantics. Phase 10 rewrites the consumer to `aurelion.events`.
- Effective Access read API (`GET /effective-grants` list, `GET /effective-grants/{id}`, `GET /effective-grants/explain`) and `ix_effective_grants_source_initiative_id` index (Phase 09 Step 4); internal: mirrored the index into `EffectiveGrant.__table_args__` and added AST contract test pinning no-LogService / no-session-mutation / no-event-emission for `EffectiveAccessReadService`.
- Effective Access batch projection driver (EffectiveAccessProjectionService) with idempotent ON CONFLICT upsert on uq_effective_grants_source_pair and eas.projection.completed event emission
- EAS projector pure function with `AccessFactView` / `InitiativeView` / `EffectiveGrantDraft` DTOs
- EffectiveGrant ORM with LIST(subject_kind) × HASH(application_id) partitioned migration

## [0.1.0] - 2026-04-18

### Added

- Phase 8 Remote Resources Normalization complete (17/17 milestones — Step 17 CLI parity added; Step 18 CLI syntax alignment cancelled and parked as deferred debt)
- ACL reference normalizer — first capability engine in `capabilities/normalization/`
- `ACLNormalizerService.ingest_and_normalize` orchestrator with SAVEPOINT-isolated duplicate handling
- Phase 08 end-to-end pipeline test (ingest → normalize → bind → idempotency)
- `AccessFactService.get_fact_by_natural_key` lookup helper with explicit IS NULL predicate (NULLS NOT DISTINCT)
- `ResourceService.get_resource_by_external_id` lookup helper
- Subject.status derivation service with CustomerService hook and `subject.status_changed` event
- ThreatFact inventory slice with PG ARRAY `active_indicators`, risk_score, and PUT upsert
- AccessUsageFact inventory slice with window uniqueness and non-negative usage_count
- OwnershipAssignment inventory slice with XOR CHECK constraint and `primary | secondary | technical` kind
- Initiative inventory slice with 9-value `InitiativeType` vocabulary and validity window
- ArtifactBinding inventory slice with at-least-one-target CHECK constraint
- AccessFact inventory slice with 5-column NULLS NOT DISTINCT uniqueness key
- AccessArtifact append-only JSONB payload slice
- Resource and ResourceAttribute with first-class `privilege_level`, `environment`, `data_sensitivity`
- Action StrEnum in shared inventory enums module
- Account service surface with `subject_id` nullable FK and `status` closed enum
- Subject inventory slice with kind/nhi_kind/principal FKs, denormalized status, and three CHECK constraints
- SubjectAttribute slice with uniqueness on (subject_id, key)
- Customer and CustomerAttribute inventory slice with attribute sub-routes
- Application.code column (NOT NULL, UNIQUE) with `get_application_by_code` repository helper
- Engineering Studio user guide section (overview, configuration, usage)
- Phase 6 Policy Decision Point (PDP) complete (19/19 milestones)
- PDP schemas (Facts, Decision, Rule, RulePack, AbstractState, RiskLevel)
- PDP evaluator with lifecycle, risk, and mapping stages
- Employee lifecycle rules (pre-hire, active, on_leave, terminated, initiatives, grace)
- NHI lifecycle rules (owner_terminated, orphaned, expired, expiring, locked, owner_on_leave)
- Customer lifecycle rules (registered, verified, active, banned, suspended, deletion_requested, trial)
- IDP subject-level rules (target=null) for employee, NHI, and customer
- ITDR risk rules (credential_compromised, impossible_travel, mfa_bombing, brute_force, session_hijack, token_replay, nhi_credential_exposed)
- Static risk rules (admin_no_mfa, prod_admin, pii_access, dormant, risk_score)
- CIAM risk rules (account_takeover, credential_stuffing, bot_detected, device_anomaly, enterprise_no_mfa)
- Mapping stage for ad, jira, github, stripe_billing, customer_portal
- Policy YAML loader and rule fixtures (lifecycle.yaml, risk.yaml, mapping.yaml)
- RULES_GUIDE.md reference for AI rule authoring
- PolicyService with log integration
- POST /api/v0/policy/evaluate endpoint
- `al policy evaluate` CLI command
- PDP architecture doc page
- Phase 5 Identity Core Domain complete (Person, Employee, EmployeeRecord, NHI vertical slices: ORM, migrations, REST APIs, tests)
- Person ORM model
- PersonAttribute ORM model
- migration for persons and person_attributes
- Person schemas (PersonCreate, PersonRead, PersonAttributeCreate, PersonAttributeRead)
- Person repository
- Person service (with LogService integration)
- Person REST API (POST/GET /persons, GET /persons/{id}, GET/POST/DELETE /persons/{id}/attributes)
- Person CLI (read-only: list, get, attributes)
- Employee ORM model
- EmployeeAttribute ORM model
- migration for employees and employee_attributes
- Employee schemas (EmployeeCreate, EmployeeRead, EmployeeAttributeCreate, EmployeeAttributeRead)
- Employee repository
- Employee service (with LogService integration)
- Employee REST API (POST/GET /employees, GET /employees/{id}, GET/POST/DELETE /employees/{id}/attributes)
- Employee CLI (read-only: list, get, attributes)
- EmployeeRecord ORM model
- EmployeeRecordAttribute ORM model
- migration for employee_records and employee_record_attributes
- EmployeeRecord REST API (POST, GET, list, attributes CRUD)
- EmployeeRecordService with log integration
- EmployeeRecord schemas and repository
- EmployeeRecord CLI (read-only: list, get, attributes)
- NHI ORM model
- NHIAttribute ORM model
- migration for nhis and nhi_attributes
- NHI schemas (NHICreate, NHIRead, NHIAttributeCreate, NHIAttributeRead)
- NHI repository
- NHIService with log integration
- NHI REST API and attribute endpoints
- NHI CLI (list, get, create, attributes, add-attribute, remove-attribute)
- Phase 4 Log Sink Foundation complete (11/11 milestones)
- LogEvent Pydantic schema and LogSink protocol in src/platform/logs/
- LogLevel enum (debug, info, warning, error, critical)
- FileLogSink provider (JSONL append-only, AURELION_LOG_FILE_PATH env)
- LogSinkFactory with file and stub providers (elk, loki, seq, zabbix, splunk, qradar, rsyslog, nagios, fluentd)
- LogService (emit_log, log_info, log_warning, log_error) with AURELION_LOG_PROVIDER env
- Log integration into SecretService and LakeBatchService
- IDM embedded in-memory log consumer for connectors using inmemory transport
- Log read API (GET /api/v0/logs) and LogReader/LogReadFactory
- Phase 3 Data Lake Foundation complete (11/11 milestones)
- Connector result ingest: inline/lake_ref only; staging bulk tables removed from active flow
- Standardized connector result ingest contract with inline and lake_ref result types
- Lake batch API and CLI (create/get/data/delete)
- LakeBatchService for lake batch create/get/read/delete
- LakeBatchCreate and LakeBatchRead schemas for lake batch metadata
- LakeBatch ORM model for data lake batch references
- DataLakeStorageFactory with file, s3, and iceberg provider registration
- FileDataLakeStorage provider for local data lake development
- DataLakeStorage interface for lake storage backend abstraction
- Connector result ingest API and PostgreSQL staging tables
- POST /applications/{id}/accounts for mock connector provisioning (AccountCreateRequest: username, email)
- MockRuntime (MQ publisher) and ApplicationRuntime protocol
- Shared event contract for connector communication
- SQLite storage for mock connector
- Mock connector consumer (event handler)
- Connected mock connector consumer via in-memory transport
- RabbitMQ transport (RabbitMQEventPublisher, process_next_event_rabbitmq, run_rabbitmq_consumer)
- Split transport/connector: platform/connectors (protocols, runtime_factory), runtimes/mock_connector
- SecretManager interface for secret provider abstraction
- FileSecretManager for development-only file-based secret storage
- SecretManagerFactory for provider resolution by name
- Stub SecretManager providers (vault, akeyless, conjur, openbao)
- Secret domain model for metadata (key, provider, namespace, timestamps)
- Secret schemas (SecretCreate, SecretRead, SecretDelete)
- SecretService for provider-agnostic secret operations
- Secret API endpoints (GET /secrets list, POST/GET/DELETE /secrets)
- Secret metadata persistence on create/delete
- Provider API (CRUD: GET/POST/DELETE /secrets/providers)
- Application ORM model for reconciliation target configuration
- ApplicationTransport interface for connector execution contract
- MQApplicationTransport for connector execution via message queue
- ApplicationTransportFactory for transport resolution
- Account ORM model for reconciliation
- Role ORM model for reconciliation
- Privilege ORM model for reconciliation
- Reconciliation DTOs (AccountDTO, RoleDTO, PrivilegeDTO) for connector payload validation
- Account reconciler for AccountDTO-to-Account reconciliation
- Role reconciler for RoleDTO-to-Role reconciliation
- Privilege reconciler for PrivilegeDTO-to-Privilege reconciliation
- Reconciliation result schema (EntityReconciliationResult, ReconciliationResult) for reconciliation counters
- Generic reconciliation engine for upsert-style DTO-to-ORM synchronization
- Application reconciliation service for full account/role/privilege reconciliation
- POST /applications/{id}/reconcile endpoint for manual reconciliation
- Integration test for full reconciliation flow
- POST /applications endpoint for creating applications
- GET /applications endpoint for listing applications
- DELETE /applications/{id} endpoint for removing applications

### Changed

- **BREAKING** `AccessFactService.create_fact` no longer calls `session.rollback()` on `IntegrityError`; caller owns transaction boundary — wrap in `session.begin_nested()` to isolate duplicates from the outer transaction

### Fixed

- AccessFact duplicate detection with NULL `account_id`: unique constraint recreated with NULLS NOT DISTINCT (PG 15+)

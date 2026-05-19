# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`StepScopedLogService` log facade for runner-driven step emits.** New `platform/orchestrator/step_scoped_logs.py` wraps the per-run `LogService` for the duration of a single step and auto-injects two things into every action-side emit: the six engine-action participant keys (`pipeline_run_id`, `step_run_id`, `engine`, `action`, `target_type`, `target_id`) when missing, and a side-channel `payload['step_run_id']` so the buffer can filter by step. The action's own `target_id` (e.g. plan id, account id) is preserved — the side-channel sits on `payload`, not `target_id`. `LogService` gains a public `sink` property and `PARTICIPANT_PAYLOAD_KEYS` is exported as a constant for the wrapper. `runner.py` wraps `logs` before passing it to `ActionContext`. 6 new unit tests.
- **`payload_step_run_id` query parameter on `GET /api/v0/log-buffer`.** Filters buffered log events by `payload->>'step_run_id'`. Accepted as a sole discriminator (no longer 400s if other params are absent). Backed by Alembic revision `a3b4c5d6e7f8` (`log_buffer_step_run_id_index`) which adds a partial expression index `ON log_event_buffer ((payload ->> 'step_run_id')) WHERE payload ? 'step_run_id'` so per-step Logs panels in product UIs no longer hit a full table scan.
- **`platform/orchestrator/cartridge_paths.py`** — single source of truth for `PIPELINE_SOURCE_DIRS` (kernel pipelines dir + `<monorepo-root>/cartridges/journey/`). Imported by both `orchestrator/deps.py::get_loaded_pipelines` and `runtimes/platform_executor_node/main.py` to avoid drift.

### Changed

- **Executor runtime now bootstraps the lake stack and shares MQ publisher between event and log sinks.** `runtimes/platform_executor_node/main.py::_run` builds `RuntimeSettingsService` → `LakeSettings` → catalog → `ensure_tables` → `LakeSessionFactory` → `set_process_lake_deps(...)` so lake-backed actions (`access_apply.execute_plan`, `effective_access.project_application`, etc.) no longer fail with "Process lake session factory not initialised" when run inside the executor. The single `AsyncRabbitMQPublisher` is reused as both the event sink and the log sink (`AURELION_EVENTS_PROVIDER=mq`, `AURELION_LOG_SINK_PROVIDER=mq`) so executor-side `pipeline.run.*` events and per-step log lines reach RabbitMQ in production. Sink defaults remain `noop` / `file` for test-safety; real deployments must flip both via env (the kernel `.env` is updated; `.env.example` documents the contract). `runtimes/platform_api/main.py` gains the matching `set_process_lake_deps(...)` call so API-side lake-backed reads work identically.
- **`access_plan` per-subject lookup simplified.** `engines/access_plan/repository.py::fetch_current_initiatives_for_subject` no longer falls back to a `JOIN access_facts` lookup — that Postgres table was dropped during the lake migration and the join started raising `relation "access_facts" does not exist` whenever an `access_plan.plan` action ran. The remaining direct `initiatives.subject_ref` path is the sole lookup.

### Removed

- **`confirm_destructive` arg removed from `access_apply.execute_plan` invocations in `cartridges/journey/{joiner,leaver,on_leave,return_from_leave}.yaml`.** `ExecutePlanArgs(extra='forbid')` rejected the arg outright — the cartridge action contract derives destructive intent from the plan content, not a per-call flag. Misleading comments in `leaver.yaml` and `on_leave.yaml` were rewritten to match the action signature.

### Changed

- **`subject_ref` on domain events now carries `Subject.id` (Slice B — subject_ref contract flip).** Every `inventory.{employee,nhi,customer}.{created,updated,bulk_upserted}` event payload now sets `subject_ref` to the kernel `Subject.id` UUID. The previous value was the principal id (employee_id / nhi_id / customer_id). Principal ids remain on the payload under their own dedicated keys (`employee_id`, `nhi_id`, `customer_id`). `NHIService._resolve_nhi_subject_id` has been removed; `ensure_for_principal` (idempotent) is called instead. `inventory.nhi.updated` and `inventory.nhi.expired` no longer carry the transient `subject_id` field. `inventory.customer.created` and `inventory.customer.updated` now carry `subject_ref` and `subject_type='customer'` for contract uniformity. The `pipelines/access_plan_subject_triggers.yaml` `inventory.nhi.expired` trigger now extracts `subject_ref` from the payload instead of `nhi_id`.

- **`GET /api/v0/subjects` — filter params and paginated envelope.** Endpoint now accepts `principal_employee_id`, `principal_nhi_id`, `principal_customer_id` (UUID, optional, AND-combined), `limit` (default 100, max 1000), `offset` (default 0). Response shape changes from `list[SubjectRead]` to `SubjectListResponse { items, total, limit, offset }`.

- **`PATCH /api/v0/org-units/{id}` replaces `PUT /api/v0/org-units/{id}` (Phase 20 M-B).** The update endpoint for external org-units now uses the PATCH verb. The request body, response schema, and status codes are unchanged; only the HTTP method changes. Kernel tests updated to use `client.patch`.

- **`OrgUnitListItem` returned by `GET /api/v0/org-units` now carries `parent_id` (Phase 20 M-D).** `parent_id` is `UUID | null` — UUID string when the org-unit has a parent, `null` when it is a root. `OrgUnitRead.parent_id` (single-row `GET /org-units/{id}`) is unchanged.

- **`EmployeeCreate` and `EmployeeRead` carry `org_unit_id` (Phase 20 M-C).** `POST /api/v0/employees` accepts `org_unit_id` (nullable UUID); the service validates the org-unit row exists before inserting (404 on unknown id). `GET /api/v0/employees` and `GET /api/v0/persons` now require `limit` and `offset` query params (calling without them returns 422) and return the envelope `{items, total, limit, offset}` with page size capped at 1000. `inventory.employee.created` event payload gains the `org_unit_id` field. **Breaking change:** bare `GET /api/v0/employees` and `GET /api/v0/persons` without pagination params no longer work.

- **`GET /api/v0/org-units` pagination (Phase 20 K-O).** `limit` and `offset` are required query params; calling the endpoint without them is a contract error (422). Response envelope is `{items, total, limit, offset}`. Page size capped at 1000. Stable ordering by `external_id ASC`. Repository function `list_all_org_units` replaced by `list_org_units_page(session, *, limit, offset) -> tuple[list[OrgUnit], int]`.

### Added

- **Auto-create Subject for every Employee, NHI, and Customer at the service layer (`SubjectService.ensure_for_principal`).** `EmployeeService`, `NHIService`, and `CustomerService` now call the new idempotent `ensure_for_principal` method post-flush on every create path, producing a `Subject` row in the same transaction. The existing reconcile-path helper `_ensure_subject_for_employee` is replaced by the same method. Alembic data migration `fa1b2c3d4e5f` backfills missing Subject rows for existing principals. (Phase Subject-A)

- **`PATCH /api/v0/employees/{id}` endpoint.** Accepts `EmployeePatch` body (any of `org_unit_id`, `description`, `attributes`). Emits one fat `inventory.employee.updated` event with `changes` map; attribute upserts surface as `changes["attributes.<key>"]`. The service method already existed; this exposes it over HTTP so Journey can drive the lifecycle joiner transition from the contractor onboarding flow.

- **Single-row CRUD on `/api/v0/org-units` (Phase 20 M-A).** POST, GET `/{id}`, PUT `/{id}`, DELETE `/{id}` for external org-units. Internal org-units are reconcile-owned — the endpoints reject mutations on them (409). New nullable `description` column on `org_units`. Alembic migration `2026_05_16_0900_phase_20_ma_org_units_description` is fully reversible.

- **`org_units.is_internal` column (Phase 20 K-N).** Adds `is_internal BOOLEAN NOT NULL DEFAULT TRUE`
  to the `org_units` table. Exposed on `GET /api/v0/org-units` responses (`OrgUnitListItem`).
  Accepted (but not yet propagated through the lake path) on `POST /api/v0/org-units/bulk`
  (`OrgUnitBulkItem`). `OrgUnitService.bulk_upsert_org_units` passes the field through to the
  repository upsert. A PL/pgSQL trigger (`trg_org_units_is_internal_consistency`) enforces that
  every node in a connected org-unit tree shares the same `is_internal` value. The trigger fires
  on every INSERT and UPDATE; any single-row flip that contradicts the current parent or any child
  is rejected. **Subtree flips of >1 node are not supported via plain UPDATE in K-N** — to convert
  a multi-node subtree, drop it and recreate it with the new value. Alembic migration
  `2026_05_15_2331_phase_20_kn_org_units_is_internal` is fully reversible.

- **Four default Journey pipeline cartridges (Phase 20 K-J).** YAML files
  shipped in `<monorepo-root>/cartridges/journey/`:
  - `joiner.yaml` (`journey.joiner`) — non-destructive null → active:
    `access_plan.plan` → `access_apply.execute_plan(confirm_destructive=false)`
    → welcome `notifications.send_email`.
  - `leaver.yaml` (`journey.leaver`) — destructive active → terminated:
    `access_plan.plan` → `notifications.send_inapp` (operator confirm
    request) → `wait_for_event` (`journey.case.apply_confirmed`, 7d
    timeout) → `access_apply.execute_plan(confirm_destructive=true)` →
    manager `notifications.send_email`.
  - `on_leave.yaml` (`journey.on_leave`) — same confirm-gated skeleton as
    leaver, ending in an SMS notification.
  - `return_from_leave.yaml` (`journey.return_from_leave`) — non-destructive
    mirror of joiner.
  All four name themselves under the `journey.*` namespace so admin tools
  can filter the cartridge picker to Journey pipelines only. Loader
  validation pass with `validate_action_refs=True` confirms every step
  references a registered action. 5 new smoke tests verifying the
  load path + structural sanity.

- **`engines/notifications/` actions and Jinja2 templates (Phase 20 K-I).** Four pipeline-callable actions register at import time via `@register_action`, all `idempotent=False`:
  - `notifications.send_email(template, to, ctx, locale, correlation_id?)`
  - `notifications.send_sms(template, to, ctx, locale, correlation_id?)`
  - `notifications.send_webhook(url, template, ctx, headers, correlation_id?)`
  - `notifications.send_inapp(template, recipient_kind, recipient_id, routing_key, ctx, link_to?, case_id?, correlation_id?)`
  Each action renders the named template via the new template engine
  (`engines/notifications/template_engine.render(channel, name, ctx)`),
  builds the channel's `Message` dataclass, and delegates to the
  factory-resolved provider. Missing templates and provider-side failures
  surface as `sent=False` with a descriptive `reason`. Initial template
  set: `email/welcome_employee`, `email/leaver_manager`,
  `inapp/leaver_confirm_required`, `sms/leave_starts`,
  `webhook/case_completed`.
- New top-level deps in `pyproject.toml`: `jinja2>=3.1.0`,
  `httpx>=0.28.1` (was dev-only). `uv.lock` regenerated.
- 11 unit tests for the template engine and the four actions
  (registry membership, file-provider round-trip per channel, missing
  template → typed failure).

- **`platform/notifications/{email,sms,webhook,inapp}/` subsystems (Phase 20 K-C/K-D/K-E/K-F).** Four sibling channels modelled on `platform/secrets/` and `platform/storage/`: each ships `interface.py` (typed Protocol + `Message` / `SendResult` dataclasses), `factory.py` (env-driven provider resolution via `AURELION_NOTIFICATIONS_<CHANNEL>_PROVIDER`), a mandatory `file` provider that appends JSON-lines records to a configurable path, and at least one real provider:
  - `email/providers/smtp.py` — SMTP via `smtplib`, configured from kernel secret store under `notifications/email/smtp/*`.
  - `sms/providers/twilio.py` — Twilio REST via `httpx`, configured under `notifications/sms/twilio/*`.
  - `webhook/providers/http.py` — generic JSON POST via `httpx`.
  - `inapp/providers/eventbus.py` — emits an MQ event on the caller-supplied `routing_key` (e.g. `notifications.inapp_journey.dispatched`) so the product-side MQ subscriber (J-G) persists a `JourneyNotification` row.
- 19 unit tests across the four subsystems (file provider + factory contract; eventbus emission for inapp).
- `PipelineDefinitionLoader.load_many(paths)` (Phase 20 K-H): scan multiple directories and merge into one dict; cross-directory `pipeline.name` collisions surface as `PipelineLoadError` exactly as intra-directory collisions do; missing directories contribute zero pipelines silently. 4 new tests.
- `_JOURNEY_CARTRIDGES_DIR` wired into `platform/orchestrator/deps.py::get_loaded_pipelines`: the kernel now picks up Journey pipeline cartridges from `<monorepo-root>/cartridges/journey/` in addition to the kernel-shipped `aurelion-kernel/pipelines/`. A missing directory is a no-op.
- `cartridges/journey/` directory tracked in the monorepo (currently `.gitkeep` only; default cartridges land in K-J).
- **Per-row event emission from reconciliation apply (Phase 20 K-B + K-G).** All four `engines/inventory_reconcile/master_data_apply::apply_*_delta` functions now accept an optional `event_service` parameter (defaults to `noop_event_service` so existing callers keep working). When wired with a real `EventService` they emit one event per successfully applied delta item:
  - `inventory.person.created` / `inventory.person.updated`
  - `inventory.org_unit.created` / `inventory.org_unit.updated`
  - `inventory.employee.created` / `inventory.employee.updated` (carries `subject_ref`, `subject_type` for access-plan trigger compatibility)
  - `inventory.account.created` / `inventory.account.updated` — `revoke` and `reactivate` operations also emit `updated` with a `status` change so downstream subscribers see the lifecycle move.
- `apply_employees_delta` now applies arbitrary `attributes` keys from the delta payload into `ent_employee_attributes` (previously only `is_locked` / `description` / `org_unit_external_id` were applied — attribute changes were silently dropped). Per-attribute deltas appear in the emitted `inventory.employee.updated` payload under `changes["attributes.<key>"]`.
- `apply_master_data_delta` dispatcher forwards the new `event_service` parameter to the underlying entity-specific function.
- 6 new unit tests covering create/update/revoke event emission per entity type, attribute-change emission for employees, and the noop default path.

### Removed

- Event keys `subject.context.changed` and `subject.employment_status.changed` removed entirely from kernel. No deprecation period — no in-tree or external consumer depended on them outside of the `access_plan_subject_triggers` pipeline, which is migrated below. Test suite assertions and docstrings rewritten to the new event shape.

### Changed

- **Unified inventory `<entity>.updated` event shape (Phase 20 K-A).** `EmployeeService.update_employee`, `NHIService.update_nhi`, and `AccountService.update_account` now each emit a single fat `inventory.<entity>.updated` event per change-bearing call. Payload contract: `{entity_id, [subject_ref, subject_type,] changes: {field: {old, new}}}`. For employees and NHIs `subject_ref` is the entity id, matching the access-plan trigger contract. Per-attribute changes appear under `changes["attributes.<key>"]`. A patch that targets fields already at the requested value emits nothing (true no-op). `inventory.account.updated` migrated from `changed_fields: [...]` to `changes: {field: {old, new}}`.
- `pipelines/access_plan_subject_triggers.yaml` rewritten (`version: 2`) to bind on `inventory.employee.updated` (with `match.changes.employment_status: {}` and `match.changes.org_unit_id: {}` containment filters) and `inventory.nhi.updated` (no extra matcher — every NHI update replans). `subject.replan.required` and `inventory.nhi.expired` bindings unchanged.
- `inventory/accounts/repository.py::update_account` return type changed from `set[str]` to `dict[str, dict[str, object | None]]` mapping field → `{old, new}`. Enum and UUID values are coerced to JSON-friendly primitives.
- `pipelines/schema.json::pipeline.name` regex relaxed to allow dot-separated namespaces (e.g. `journey.joiner`).

- `PipelineDefinitionLoader.load_many(paths)` (Phase 20 K-H): scan multiple directories and merge into one dict; cross-directory `pipeline.name` collisions surface as `PipelineLoadError` exactly as intra-directory collisions do; missing directories contribute zero pipelines silently. 4 new tests.
- `_JOURNEY_CARTRIDGES_DIR` wired into `platform/orchestrator/deps.py::get_loaded_pipelines`: the kernel now picks up Journey pipeline cartridges from `<monorepo-root>/cartridges/journey/` in addition to the kernel-shipped `aurelion-kernel/pipelines/`. A missing directory is a no-op.
- `cartridges/journey/` directory tracked in the monorepo (currently `.gitkeep` only; default cartridges land in K-J).

## [0.12.0] - 2026-05-13

### Removed

- `AccountService.upsert_bulk` and `repo_upsert_accounts_bulk` (legacy PG-direct path; replaced by lake-first in H10). Cleanup: unused vars in `scripts/seed_phase19_demo.py` (`manager_subj`, `rule_admin_id`, `artifact_ids`).

### Changed

- `POST /accounts/bulk` migrated from direct PG write to lake-first pattern: handler now delegates to `AccountLakeService.upsert_batch` and returns `{row_count, snapshot_id}` instead of `{upserted}`. PG is populated exclusively via the master-data reconcile+apply flow.
- `AccountBulkItem` schema extended with `external_id`, `status`, `is_privileged`, `mfa_enabled`, `meta` fields (all optional, nullable).
- `AccountBulkResponse` shape changed: `upserted: int` → `row_count: int, snapshot_id: int | None`.

### Added

- `raw.accounts` Iceberg table schema + provisioning: `RAW_ACCOUNTS_SCHEMA` / `RAW_ACCOUNTS_TABLE` / `RAW_ACCOUNTS_PARTITION_SPEC` in `platform/lake/schemas.py`; table added to `_TABLE_SPECS` in `provisioning.py` (auto-created on startup).
- `AccountLakeService` in `src/inventory/accounts/lake_service.py`: `upsert_batch(items, ingest_batch_id)` writes to `raw.accounts` with retire-on-re-upload (composite natural key `application_id::username` encoded in `_natural_key_hint` column).
- `run_accounts_reconciliation` in `src/engines/inventory_reconcile/master_data_pipeline.py`: lake→PG delta computation for accounts; reuses `AccountHandler.compute_delta` for operation classification (create/update/revoke/reactivate/noop).
- `run_master_data_reconciliation` dispatch table extended with `account` entity_type.
- Seed script `scripts/seed_account_reconcile.py` rewritten to lake-first flow: uploads raw rows via `AccountLakeService`, then triggers `run_master_data_reconciliation` to generate delta items automatically.
- 10 new tests: 5 `AccountLakeService` unit tests (`test_account_lake_service.py`), 5 accounts pipeline tests (`test_accounts_pipeline.py`).

- `batch_application_display_by_code(session, codes)` helper in `src/inventory/display_lookups.py` — resolves `{code: ApplicationDisplay}` via `Application.code IN (...)` for plan items that store a short code string instead of a UUID.
- `GET /api/v0/accounts` now returns three enriched display fields per account: `application_code`, `application_name` (resolved from `applications` table via batch lookup), and `subject_display` (resolved via subjects → employees → persons / nhis). Fields are nullable; zero N+1 queries (two SELECTs max per request).
- `PlanItem` code-based application reverse-lookup: `enrich_plan_items` in `engines/access_plan/display_enrichment.py` now resolves `application_name` for items where `application` stores a short code (e.g. `"GHE"`) by calling `batch_application_display_by_code`. UUID-based and code-based lookups run in parallel via `asyncio.gather`. Previously these items returned `application_name=null`.

### Changed

- `batch_application_display` in `src/inventory/display_lookups.py` now returns `dict[UUID, ApplicationDisplay]` (dataclass with `code: str` and `name: str`) instead of `dict[UUID, str]`. Enables callers to populate a new `application_name` field alongside the existing `application_code` in one SQL query.
- `AccessFactRead`, `ReconciliationDeltaItemRead`, `PlanItemRead` gain `application_name: str | None` (nullable; resolved by the enrichment layer via `ApplicationDisplay.name`). Backward-compatible.
- `AccountRead` schema gains three nullable display fields: `application_code`, `application_name`, `subject_display` (all default `None`). Backward-compatible.

### Added

- Account entity reconciliation: `account` value added to `ReconciliationEntityType` enum (migration `a3f7c2d891e0`); `AccountHandler.compute_delta` (create/update/revoke/reactivate/noop) in `handlers/account.py`; `apply_accounts_delta` in `master_data_apply.py` writes Account rows to `ent_accounts` PG table; `apply_master_data_delta` dispatcher routes `account` entity_type to the new apply function; display enrichment extended to resolve username from `account_id` / `entity_id` / after_json for account delta items; seed script `scripts/seed_account_reconcile.py` seeds 6 pending account delta items (2 create, 2 update, 1 revoke, 1 reactivate); 13 new tests.

- Display fields in three read endpoints for Engineering Studio Access State view (human-readable names alongside UUIDs, resolved via batch PG lookups — never N+1):
  - `GET /api/v0/inventory-reconciles/delta-items` — `ReconciliationDeltaItemRead` gains `subject_display`, `account_display`, `resource_display`, `application_code`, `change_summary` (nullable; resolved by `enrich_delta_items` in new `display_enrichment.py`).
  - `GET /api/v0/plans/items` — `PlanItemRead` gains `subject_display`, `application_code`, `target_display`, `change_summary` (nullable; resolved by `enrich_plan_items` in `engines/access_plan/display_enrichment.py`).
  - `GET /api/v0/access-facts` — `AccessFactRead` gains `subject_display`, `account_display`, `resource_display`, `application_code` (nullable; resolved by `enrich_access_facts` in `inventory/access_facts/display_enrichment.py`).
  - Shared batch lookup helpers in `src/inventory/display_lookups.py` (`batch_employee_display`, `batch_nhi_display`, `batch_account_display`, `batch_resource_display`, `batch_application_display`, `batch_subject_display`).
  - 49 new tests across `test_display_lookups.py`, `test_display_enrichment.py` (reconcile, access_plan, access_facts).
  - Backward-compatible: all new fields are nullable with `None` as fallback when the referenced entity is not found.

- Flat plan items list endpoint + count for ES Access State view: `GET /api/v0/plans/items` (flat list of PlanItems with JOIN on PlanItemExecution and AccessPlan; filters: execution_status CSV, plan_status, kind, application, plan_id, subject_ref, subject_type; limit 1–200, default 50; offset pagination; returns `PlanItemListResponse`) and `GET /api/v0/plans/items/count` (same filters, returns `{"count": N}`). Repository: `list_plan_items_cross_plan` and `count_plan_items_cross_plan` with three-table JOIN. Schema: `PlanItemRead`, `PlanItemListResponse`, `PlanItemCountResponse`. 14 new tests in `test_plan_items_list.py`. Routes declared before `/{plan_id}` to avoid FastAPI path collision.

- Cross-run delta items list endpoint + count endpoint for ES Access State view: `GET /api/v0/inventory-reconciles/delta-items` (keyset-paginated flat list across all runs, filters: status, application_id, entity_type, subject_id, account_id, resource_id, operation; limit 1–200, default 50; returns `application_id` per item) and `GET /api/v0/inventory-reconciles/delta-items/count` (same filters, returns `{"count": N}` for UI badge). Repository: `list_delta_items_cross_run` and `count_delta_items_cross_run` with JOIN on `ReconciliationRun`. Schema: `ReconciliationDeltaItemRead` gains nullable `application_id` field; new `DeltaItemCountResponse`. 11 new tests in `test_cross_run_delta_items.py`.

### Changed

- Phase 19 A2 (cleanup): old `sync_apply` package removed — directory `src/engines/sync_apply/` deleted; router, conftest, executor preload, pipeline YAML, and all cross-references migrated to `inventory_sync`.

### Added

- Phase 19 H2 (final gate): test isolation helpers — `_register_if_absent` on `ActionRegistry`; `_ensure_plan_registered` / `_ensure_execute_plan_registered` / `_ensure_scan_for_replan_registered` module-level helpers for registry recovery after `_clear_for_tests`; `PipelineDefinitionLoader(validate_action_refs=False)` flag for tests that verify pipeline structure without registry state. Test suite: 3584 passed, 0 failed, 4 skipped in full run.
- Phase 19 H2 (gate fixes): `test_no_legacy_residuals.py` now skips sibling guard files; `test_lake_writer_imported_only_from_sync_apply` accepts both `inventory_sync/` and `sync_apply/` as valid lake_writer owners; `test_sync_single_fact.py` fixture key renamed `role_id` → `target_id` (domain data, not legacy schema column).

- Phase 19 G8: E2E NHI scenarios — new NHI → birthright plan → apply → verify; `nhi.expired` → revoke plan; `application.decommissioned` → fan-out N NHI revoke plans. New `src/engines/access_plan/tests/test_e2e_g8_nhi.py` (3 tests). `test_nhi_new_birthright_plan_apply_verify`: seeds NHI record (service_account, nhi_status=active, application_id) + Subject(kind=nhi); POST /plans → 201 (PDP applies NHI birthright rules with subject.kind=nhi + attributes.nhi_status=active → account_create + grant_role items); POST /plans/{id}/apply → 201 + lease; execute_plan stub connector (mismatch→match); all items done, sync_single_fact per item, Initiative with origin=policy_rule:nhi_birthright_*, lease deleted, plan active. `test_nhi_expired_revoke_plan`: seeds NHI + initiatives (simulating prior apply); `NHIService.deactivate_nhi` sets is_locked=True + emits `inventory.nhi.expired`; updates nhi_status attribute to 'expired'; replan (patched effective grants) → PDP produces empty desired (nhi_status!=active → no rule matches) → revoke all; POST /apply with confirm_destructive=true → 201; execute_plan: all revoke items done, sync_single_fact per item. `test_application_decommissioned_fanout_nhi_revoke`: seeds Application + 3 NHIs + Subjects; `decommission_application` sets is_active=False + emits `inventory.application.decommissioned`; `fanout_replan_for_application_action` (via direct call with ActionContext) creates one plan per NHI (3 plans); verifies nhi_count=3, plans_created=3, idempotency_keys contain application_id; second fanout run → same keys → idempotent reuse (plans_created=3 again). New methods: `NHIService.update_nhi(session, id, NHIPatch)` + `NHIService.deactivate_nhi(session, id)` in `inventory/nhi/service.py`; `NHIPatch` schema in `inventory/nhi/schemas.py`; `decommission_application(session, id, event_service)` in `platform/applications/service.py`; `resolve_subject_ref_for_nhi(session, nhi_id) → str|None` in `access_plan/repository.py`; `fanout_replan_for_application_action` fixed to resolve Subject.id via `resolve_subject_ref_for_nhi` before calling `create_plan` (previous code incorrectly passed NHI.id as subject_ref). `GenerativePDPService.__init__` rule_pack parameter made optional (default=RulePack()) for action-path usage. 3/3 G8 tests green; 11/11 E2 tests green (nhi.expired + application.decommissioned). ruff clean.

- Phase 19 G7: E2E test — initiative with future `valid_from` → scanner picks up in window → replan → apply → verify. New `src/engines/access_plan/tests/test_e2e_g7_scheduled_valid_from.py` (2 tests). `test_future_valid_from_scanner_replan_apply`: frozen clock (T0=2026-06-01T12:00Z); seeds Employee X + Initiative (type=requested, valid_from=T0+2s — future at T0); service.create_plan(now=T0) with patched `_initiatives_to_current_initiatives` → 0 plan items (PDP._is_active_initiative filters future initiative); `run_scan_for_replan(now=T0+3s, lookback_seconds=1)` — window=[T0+2s, T0+63s] — finds the initiative, emits `subject.replan.required` with stable `idempotency_key = sha1(subject_ref:bucket)`; second scanner run with same clock → same idempotency_key (bucket stable within minute); service.create_plan(now=T0+3s, idempotency_key=from_event) → initiative now active → plan has items; supersedes_plan_id = T0 plan; POST /plans/{id}/apply → 201; execute_plan stub connector (mismatch→match); all items done, sync_single_fact per item, lease released, plan active, original seeded initiative preserved (NO DELETE). `test_scanner_lookback_override_excludes_old_initiative`: seeds initiative with valid_from=T0-30min; scanner(now=T0+3s, lookback=1) window=[T0+2s, T0+63s] — old initiative outside window → no event emitted for that subject. Both tests use frozen datetime constants (no asyncio.sleep). `noop_event_service` patched at `src.platform.events.service` for event capture (ActionContext is frozen+slots, cannot inject via attribute). 2/2 green, 148/148 access_plan tests green. ruff clean.
- Phase 19 G6: E2E tests — delegated + grace initiative carry-over. New `src/engines/access_plan/tests/test_e2e_g6_delegated_grace.py` (2 tests). `test_delegated_initiative_carry_over`: seeds Employees A (delegator) + B (delegatee) + delegated Initiative for B with `origin=<A_subject_ref>`; POST /plans for B with empty birthright rule pack → PDP carry-over produces `delegation:<A_subject_ref>` origin via `_format_carry_over_origin`; plan item has type=delegated + correct source_initiative_id; execute_plan → items done, F3 chain creates Initiative with `delegation:` prefix in DB. `test_grace_initiative_carry_over`: seeds Employee C + closed requested Initiative (valid_until=past, simulating revoke) + grace Initiative (valid_until=now+7days, origin=`grace:<requested_id>`); POST /plans for C → PDP carry-over picks up the active grace initiative → plan item has type=grace origin=`grace:<grace_initiative_id>`; execute_plan → done; initiative chain verified: grace.origin → original_requested_id; both seeded initiatives preserved (NO DELETE, audit trail). Both tests patch `_initiatives_to_current_initiatives` to supply application + target_descriptor (not stored in Initiative table — carry-over supply pattern). 2/2 green, 146/146 access_plan tests green. ruff clean.
- Phase 19 G5: E2E test — terminated employee → revoke plan → apply → verify. `test_terminated_employee_revoke_plan`: seeds OrgUnit/Person/Employee/EmployeeAttribute(employment_status=active)/Subject/ConnectorInstance + 2 Initiative rows (simulating prior-apply grants via `subject_ref` denormalized column); patches employment_status → `terminated` via `EmployeeService.update_employee` (emits `subject.employment_status.changed`); patches `fetch_current_effective_grants` / `count_current_effective_grants` to return 2 fake EffectiveGrant objects; calls POST /plans (MQ matcher simulation) — PDP.generative with `terminated` status returns empty desired state (no birthright rules match, carry-over blocked by `_employment_blocks_carry_over`) → diff = 2 revoke items → `requires_confirmation=True` (100% > 50% threshold); asserts POST /plans/{id}/apply without `confirm_destructive` → 422 `destructive_threshold_exceeded`; POST with `confirm_destructive=true` → 201; `execute_plan` with stub connector + stub sync: all items done, `sync_single_fact(op=revoke)` called per item, referenced initiatives closed (`valid_until` set — NO DELETE), lease released, plan active. `test_terminated_empty_grants_no_confirmation`: terminated employee with 0 grants → empty plan → `requires_confirmation=False`. Repository fix: `fetch_current_initiatives_for_subject` now executes two queries (legacy JOIN path + direct `subject_ref` lookup) and merges/deduplicates results — enables Phase 19 F3+ initiatives (stored with `subject_ref`) to participate in revoke planning without requiring PG `access_facts` shim rows.
- Phase 19 G4: E2E test — existing employee, context changed → replan supersedes old plan. `test_context_change_replan_supersedes`: seeds employee (role=engineer) + initial plan (account_create + grant_role), patches attributes via `EmployeeService.update_employee` (role→senior_engineer; emits `subject.context.changed`), calls `create_plan` again simulating MQ matcher trigger → new plan created with `supersedes_plan_id=old_plan.id` and old plan set to `status=superseded`. POST /plans/{new_id}/apply → 201 + lease; `execute_plan` with stub connector; verify PlanItemExecution=done, sync_single_fact per item, new Initiative rows, AccessApplyActive lease deleted, new plan active, old plan superseded, supersedes chain intact. `EmployeePatch` schema added to `inventory/employees/schemas.py`. `EmployeeService.update_employee` method added: patches `org_unit_id`/`description`/`attributes`, emits `subject.context.changed` on context-changing changes and `subject.employment_status.changed` on employment_status attribute change.
- Phase 19 G3: E2E test — new employee → plan → apply → verify. `test_new_employee_plan_apply_verify` covers full scenario: seed Person/Employee/EmployeeAttribute/Subject/OrgUnit/ConnectorInstance, POST /plans with birthright RulePack (account_create + grant_role rules), assert 201 + items + decision_snapshot + PlanDependency, POST /plans/{id}/apply (201 + lease), simulate execute_plan with stub connector (preflight mismatch → call → post-verify match), verify all PlanItemExecution.status=done, sync_single_fact called per item, Initiative rows with origin=policy_rule:*, AccessApplyActive lease deleted, plan status=active. `test_apply_idempotent_same_plan` verifies POST /plans/{id}/apply on active run → 200 + same pipeline_run_id. Bug fix: `AccessPlanService._build_subject_context` now resolves `principal_employee_id`/`principal_nhi_id` via `fetch_subject_principal_ids(subject_id)` before calling `fetch_employee_context_data`/`fetch_nhi_context_data` (previously passed Subject.id as Employee.id). New `fetch_subject_principal_ids` helper in `access_plan/repository.py`.
- Phase 19 F3: Post-success chain in `access_apply.execute_plan`. Grant path: `inventory_sync.sync_single_fact(descriptor, op=grant, event_key)` (lake write first) + `inventory.initiatives.create_or_get(access_fact_id, type, origin)` idempotent upsert (PG second). Revoke path: `sync_single_fact(op=revoke)` + `initiative.close(initiative_id, valid_until=now())` for each `initiative_id` in `PlanItem.initiative_refs` — NO DELETE, audit trail preserved. Crash recovery: `executing` items with preflight match re-run F3 chain idempotently (`event_key` check skips lake duplicate; `create_or_get` returns existing; `close` on already-closed is no-op). Atomicity: lake write always first; PG commit (Initiative + PlanItemExecution.done + auto-invalidation) in one transaction. New `src/engines/access_apply/f3_chain.py` — `run_f3_chain()` entry point; `sync_single_fact` called via `asyncio.to_thread`. `InitiativeService.create_or_get()` (idempotent upsert by `(access_fact_id, type, origin)`) and `InitiativeService.close()` (idempotent `valid_until=now()` update + event) added to `inventory/initiatives/service.py`. `get_by_unique_key()` helper added to `inventory/initiatives/repository.py`. `execute_plan` signature extended with optional `sync_service` / `initiative_service` (None = F3 skipped, safe for focused F1 tests). `execute_plan_action` wires all three services. 6 integration tests in `test_execute_plan_f3.py`; 4 service tests in `test_service.py` (create_or_get idempotency + close + no-delete assertion).
- Phase 19 E4: Scheduled replan scanner. `inventory.initiatives.scan_for_replan` action — full implementation replacing E1 stub. Stateless scanner queries initiatives whose `valid_from` or `valid_until` falls in `[now() - lookback, now() + 60s]`; emits `subject.replan.required` event per unique subject with `idempotency_key = sha1(subject_ref:window_bucket)` (stable within 1-minute window bucket). Deduplication: overlapping runs within same minute produce identical key — matcher (E3) collapses via `(pipeline_name, idempotency_key)` unique constraint. `scanner_window_lookback_seconds` added to `RuntimeSettingsConfig` (default 120s, DI-overridable in tests). `subject_ref` + `subject_type` nullable columns added to `initiatives` table (denormalized for scanner performance; F3+ populates). `idx_initiatives_replan_horizon` composite index on `(valid_from, valid_until)`. Alembic migration `b1be83106f6e`. Routing key renamed `subject.scheduled_replan_required` → `subject.replan.required` (3-segment format required by EventEnvelope validator). 13 tests in `test_scanner_e4.py`. ruff clean.
- Phase 19 F1: `access_apply.execute_plan(plan_id)` action — full implementation replacing E1 stub. Iterates `PlanItems` in topological DAG order (Kahn's sort on `PlanDependency` edges). Per-item loop: skip if `done`; preflight `verify_fact` → if match: auto-invalidate other active plans for subject + mark done; else: set `executing`, call connector, post-verify → if match: auto-invalidate + mark done, else mark `failed` with `verify_mismatch` or `verify_timeout`. Connector error → `failed` with `apply_error`. Restart semantics: `done` items skipped; `executing` items with preflight match treated as recovered (F3 chain stub called for idempotent doz-fill); `executing` with preflight mismatch → connector retried. Auto-invalidation: same-TX `UPDATE access_plans SET status='invalid', invalidation_reason='stale_after_apply', invalidated_by_plan_id=X WHERE subject_ref=subject AND status='active' AND id != X`. Finally block: `DELETE FROM access_apply_active WHERE pipeline_run_id = run_id` + commit (lease released even on exception). F3 chain extension points marked with `# F3-EXTENSION` comments — wired fully in F3 step. New helpers in `access_plan/repository.py`: `fetch_plan_by_id`, `fetch_plan_items_ordered`, `fetch_plan_deps`, `fetch_item_executions`, `upsert_item_execution`, `invalidate_other_active_plans`, `delete_apply_lease`. 12 tests in `test_execute_plan_f1.py`.
- Phase 19 E3: MQ matcher integration for `access_plan`. Three pipeline YAML definitions: `access_plan_subject_triggers.yaml` (MQ triggers on `subject.context.changed`, `subject.employment_status.changed`, `subject.scheduled_replan_required`, `inventory.nhi.expired` → `access_plan.plan`), `access_plan_initiative_changed.yaml` (`inventory.initiative.changed` → `access_plan.fanout_replan_for_initiative`), `access_plan_application_decommissioned.yaml` (`inventory.application.decommissioned` → `access_plan.fanout_replan_for_application`). Two new actions in `access_plan/actions.py`: `fanout_replan_for_initiative` resolves subject_ref from args and calls `create_plan` once (graceful skip if subject_ref absent); `fanout_replan_for_application` queries all NHI by `application_id` and calls `create_plan` N times with per-NHI idempotency keys (`{application_id}:{nhi_id}`). `list_nhi_by_application_id` added to NHI repository. `inventory.initiative.changed` event added to `InitiativeService` on create and update (alongside existing `created`/`updated` events). Deduplification via `idempotency_key` from payload (orchestrator unique constraint). 15 tests in `test_matcher_e3.py`. ruff clean.
- Phase 19 E1: REST endpoints for `access_plan` engine. Five endpoints: `POST /plans` (201 new plan, 200 idempotency reuse, 404 subject not found), `POST /plans/dry-run` (200 with rollback — what-if analysis), `GET /plans` (pagination + filters: subject_ref, subject_type, status; max limit 100), `GET /plans/{id}` (plan + items + deps + executions), `POST /plans/{id}/apply` (201 new pipeline run, 200 reuse same plan, 404 not found, 409 plan_not_active / apply_in_progress_for_subject, 422 destructive_threshold_exceeded). Apply flow: advisory-safe INSERT into `access_apply_active` ON CONFLICT DO NOTHING; defensive stale-row cleanup with one-level retry. Action registration: `access_plan.plan` (wrapper over `AccessPlanService.create_plan`), `access_apply.execute_plan` (stub for F1), `inventory.initiatives.scan_for_replan` (stub for E4). YAML pipelines: `access_apply_pipeline.yaml` (1 step: `access_apply.execute_plan(plan_id)`), `initiatives_scheduled_replan_scan.yaml` (schedule every 1m). `pyproject.toml` per-file-ignores: B008 for `**/routes.py`. 22 API tests in `test_routes_e1.py`. ruff clean.
- Phase 19 D4: `access_plan` DAG resolver — new `src/engines/access_plan/dag_resolver.py` with `resolve_dag(plan_id, items, descriptor_map, current_account_states) → DAGResult`. Reads `dependency_rules` from connector operation descriptors; builds directed edges between PlanItems (e.g. `grant_role → account_create`). Cross-application dependencies supported via `OperationDependencyRule.application` field. Cascade expansion: `ConnectorCapabilityDescriptor.cascades.before_disable` rules inject synthetic revoke items before `account_disable`. Cycle detection via Kahn's algorithm — cycle → `requires_confirmation=True` + items in cycle marked unsatisfiable. Unsatisfiable marking: when required dep absent from plan AND current state doesn't satisfy → item ID added to `unsatisfiable_item_ids`. New `AccountDisableCascadeRule` and `AccountDisableCascades` Pydantic models added to `registration_schemas.py`. New `OperationDependencyRule.application` optional field for cross-app deps. New `repository.insert_plan_dependencies` and `repository.fetch_connector_descriptor` helpers. `AccessPlanService.create_plan` integrated at step 7c/7d: fetches descriptors + account states, runs DAG, persists `PlanDependency` rows, sets `requires_confirmation` on cycle. 13 unit tests in `test_dag_resolver_d4.py` covering linear chain, parallel branches, cycle detection, cross-app dep, cascades, unsatisfiable, multi-cascade, no-descriptor fallback. ruff clean.
- Phase 19 D3: `access_plan` account status reasoning — new `src/engines/access_plan/status_resolver.py` with `resolve_account_op_kind`, `resolve_fact_op_kind`, `resolve_item_kind` pure functions. Converts abstract diff kinds (`add_fact`/`remove_fact`) to concrete `PlanItemKind` values (`account_create`, `account_invite`, `account_activate`, `account_suspend`, `account_disable`, `grant_role`, `revoke_role`, `group_add`, `group_remove`, `entitlement_attach`, `entitlement_detach`) based on current `AccountStatus` and connector `AccountStatusTransitions`. `not_exists` sentinel for absent account row. `create→invite` preference for `not_exists→active` vs `not_exists→invited`; `suspend→disable` preference for remove path. `_apply_status_reasoning` async integration in `AccessPlanService.create_plan` (step 7b). New `repository.fetch_account_status_for_subject` and `repository.fetch_connector_transitions` helpers. 34 unit tests in `test_status_resolver_d3.py`. ruff clean.
- Phase 19 E5: Stale apply lease cleanup. `access_plan.cleanup_stale_apply_leases` action + `pipelines/access_apply_active_cleanup_scan.yaml` (schedule every 1m). `platform/orchestrator/repository.py` — read-only `get_pipeline_run_status` / `is_terminal` helpers (Phase 18 read API). `RuntimeSettingsConfig.max_apply_duration_seconds` (default 3600s). Action scans all `access_apply_active` rows: terminal pipeline run or not found → DELETE; running but `started_at < now() - max_apply_duration` → log WARN + DELETE. 10 unit tests in `test_cleanup_e5.py`.
- Phase 19 G2: Second mock connector (`mock_connector_hierarchical.py`) with hierarchical group model — `HIERARCHICAL_CONNECTOR_DESCRIPTOR` (all 11 operations; `grant_role` has clearance-level conditional `dependency_rule` on `subject_attribute`; 6 `account_status.transitions` including `suspended→disabled`); `HierarchicalConnectorState` with nested `group_members` graph, `subject_attributes`, `resolve_members(transitive)`, `is_member`, cycle-detection helpers; `HierarchicalConnectorHandler` enforcing clearance gate on `grant_role` and supporting hierarchical `group_add`/`group_remove`/`verify_fact`. 60 tests in `test_mock_connector_hierarchical.py` covering descriptor parse, 3-level transitive membership, cycle detection, conditional grant (allowed / blocked / attribute-fallback), explicit invite→activate flow, `suspend→disable` path, `verify_fact` match/mismatch/timeout for all 4 kinds. ruff clean.
- Phase 19 G1: Mock connector (first reference connector, plain flat structure). `MOCK_CONNECTOR_DESCRIPTOR` with all 11 operations (`account_create`, `account_invite`, `account_activate`, `account_suspend`, `account_disable`, `grant_role`, `revoke_role`, `group_add`, `group_remove`, `entitlement_attach`, `entitlement_detach`); `dependency_rules` per operation (role/group/entitlement require active account); `account_status.transitions` (5 transitions: `not_exists→invited`, `invited→active`, `active→suspended`, `suspended→active`, `active→disabled`); `verify_fact_supported: true`; `supported_fact_kinds: [account, role, group, entitlement]`. `MockConnectorState` (in-memory), `MockConnectorHandler` (async dispatch + `verify_fact` returning `match | mismatch | timeout`). 47 tests in `src/platform/connectors/tests/test_mock_connector.py`.
- Phase 19 F2: `sync_single_fact(descriptor, op, event_key)` method on `SyncApplyService` — wire-level idempotent single-fact append to `normalized.access_facts`. Preflight DuckDB scan checks `event_key` before any write; duplicate key → no-op (returns False). `FactDescriptor` and `SingleFactSyncOp` Pydantic types added to `inventory_sync/schemas.py` as shared cross-slice contract. `SingleFactRow` dataclass and `append_single_fact_row` / `check_event_key_exists` low-level helpers added to `lake_writer.py`. `event_key TEXT` nullable column added to `normalized.access_facts` Iceberg schema (field_id=18); `_build_arrow_table` and `append_single_fact_row` skip columns absent from actual table schema for backward compat.
- Phase 19 D2: `access_plan` service — full planning logic in `AccessPlanService.create_plan()`: reads `access_effective` (current_facts) + `inventory.initiatives` (current_initiatives) + inventory employee/NHI metadata, builds `SubjectContext`, calls `GenerativePDPService.assess()` (stateless PDP), diffs desired vs effective into `add_fact` / `remove_fact` / `modify_fact` abstract items; idempotency_key reuse and content_hash dedup (5s window); auto-supersedes older active plans; safe-revoke threshold check (`RuntimeSettingsConfig.safe_revoke_threshold`, default 0.5); advisory lock (`pg_advisory_xact_lock`) on subject_ref for consistent supersedes; emits `access_plan.plan.created` event. New `repository.py` with subject context fetchers, grant/initiative readers, dedup and supersedes helpers. `safe_revoke_threshold` field added to `RuntimeSettingsConfig`.
- Phase 19 D1: `access_plan` ORM models (`AccessPlan`, `PlanItem`, `PlanDependency`, `PlanItemExecution`, `AccessApplyActive`) with PG enums `access_plan_status`, `plan_invalidation_reason`, `plan_item_kind`, `plan_item_execution_status`, `plan_item_failure_reason`; partial indexes on `(subject_ref, status) WHERE status='active'`, `supersedes_plan_id`, and `idempotency_key WHERE NOT NULL`; Alembic migration `5c768d47065f`
- `AccountStatus.invited` value added to `account_status` PG enum (Phase 19 D1)

## [0.11.0] - 2026-05-12

### Added

- Phase 18 Native Pipeline Orchestrator complete (36/36 milestones)
- Chaos test: crash-and-resume idempotency — `effective_grants` row count identical after reclaim
- Race test: two executor nodes × 4 slots × 100 pending runs
- `reset_process_lake_deps_for_tests()` public helper in `platform/lake/factory.py` — safe teardown function for tests; prevents private-global mutation in test suites
- `pipelines/application_sync.yaml` — 6-step pipeline (reconcile → fan-out master_data_apply ×3 → sync_apply → project_eas); MQ trigger on `connector.result.received` with `args_from_payload`; `schema_version: 1`; `args.now` for projection timestamp (Phase 18 Step 21)
- `args_from_payload` property added to `trigger_mq` in `pipelines/schema.json` (enables payload extraction at trigger time)
- Smoke e2e test `src/platform/orchestrator/tests/test_application_sync_smoke.py` — full pipeline drive via matcher_tick + runner loop; asserts 6 step_runs, ≥1 effective_grant, ≥1 `inventory.access_fact.*` event
- `provisioning` engine actions `create_account` and `delete_account` registered in `ACTION_REGISTRY` (`idempotent=True`); process-level `ConnectorClient` factory in `platform/connectors/factory.py`
- `sync_apply.apply` engine action (`idempotent=True`) with `SyncApplyApplyArgs` / `SyncApplyApplyResult` schemas
- `effective_access` projection actions: `project_access_fact`, `project_application`, `apply_incremental_change` (`idempotent=False`); `ProjectAccessFactArgs`, `ProjectApplicationArgs`, `ApplyIncrementalChangeArgs`, `ProjectionResult` schemas
- `reconciliation.run` and `reconciliation.master_data_apply` engine actions with request-less lake dep factory (`platform/lake/factory.py`)
- Liveness heartbeat (`executor.process.heartbeat`) published every `EXECUTOR_HEARTBEAT_SECONDS` (default 60s, min 1s) from `platform_executor_node`; payload schema `ExecutorHeartbeatPayload`; routing key on `aurelion.events` exchange (Phase 18 Step 20)
- `POST /pipeline-runs/{run_id}/retry` endpoint with `PipelineOrchestratorService.create_retry`; `RunNotRetryableError` for non-terminal and cancelling sources; `RetryPipelineRunResponse` schema
- `POST /pipeline-runs/{run_id}/cancel` endpoint with 5-branch dispatch; `cancelling` watcher in `_heartbeat_refresher`; `asyncio.wait(FIRST_COMPLETED)` cancel path; `pipeline.run.cancelled` event
- Pipeline matcher — async MQ consumer resolving `pipeline_event_waiters` (JSONB containment) and firing MQ-triggered pipelines; single-replica via advisory lock; independent transactions per effect
- `find_matching_waiter_step_ids` in orchestrator service — JSONB `<@` containment query with status guard
- `resolve_pipeline_event_waiter` transition corrected to `awaiting_event → pending`
- MQ-trigger `args_from_payload` semantic validation in loader
- `orchestrator_matcher_queue` and `orchestrator_matcher_bindings` bootstrap settings
- Beat timeout sweep — expired `pipeline_event_waiters` trigger `failed_timeout` on step and run with `error='event_timeout'`; sweep runs inside the existing beat tick under the same `pg_try_advisory_lock`; bounded batch of 100 waiters per tick (`_EXPIRY_SWEEP_BATCH_SIZE`). `BeatTickResult` gains `expired_run_ids` and `expire_failure_count`. (Phase 18 Step 16)
- Beat schedule-firing task in `platform_api` lifespan with `pg_try_advisory_lock` multi-replica safety (`platform/orchestrator/beat.py`)
- Shared duration parser `_durations.py` extracted from runner (package-private; reused by beat)
- `croniter>=2.0.0` dependency (Apache-2.0) for cron expression parsing
- Event routing keys (Phase 18 orchestrator): `pipeline.run.created`, `pipeline.run.started`, `pipeline.run.completed`, `pipeline.run.failed`, `pipeline.run.cancelled`, `pipeline.run.heartbeat_lost`; `pipeline.step.started`, `pipeline.step.completed`, `pipeline.step.failed`, `pipeline.step.aborted`; `executor.process.heartbeat`; `connector.result.received` (Steps 7, 10, 12a, 13, 18, 20)
- CLI kernel-side note: `al pipelines list/show/run` and `al pipelines runs list/get/cancel/retry` client commands shipped in `aurelion-cli` (Steps 24a–24b); detailed CLI changelog in `aurelion-cli/CHANGELOG.md`

### Fixed

- test infra: fix pre-existing `_make_access_fact` helper in `engines/effective_access/tests/test_repository.py` to INSERT a real row into the `access_facts` shim table instead of returning a bare UUID; also fix test 5 to capture `subject_id` and call `_make_access_fact` per resource (25 tests now pass)

### Removed

- `bulk_approve_run_pending_items` from `engines/reconciliation/repository.py` (relocated to `engines/sync_apply/repository.py`; zero behaviour change).
- `engines/lake_migration` slice retired (Phase 17 Step 13 — one-shot PG → Iceberg migration tool, completed in all deployed environments). Removed: `POST /api/v0/lake-migrations` and `GET /api/v0/lake-migrations/{id}` and `GET /api/v0/lake-migrations` endpoints. Database table `lake_migration_runs` and enums `lake_migration_dataset` / `lake_migration_status` and partial unique index `uq_reconciliation_delta_items_pg_migration` dropped via Alembic revision `c2f5a8d91b04` (down_revision `b1e4f7c20d83`). `lake_batches` and `platform/lake` are unaffected.
- `LogEvent.event_type` schema field — deprecated since Phase 10 Step 23; hard-removed. `LogEvent` no longer carries an `event_type` field; `extra='forbid'` makes stale producers fail loudly at the consumer.
- `log_event_buffer.event_type` DB column — dropped via Alembic migration `phase_17_step_04_drop_log_event_buffer_event_type`. Historical values are not recovered on downgrade (downgrade re-adds the column with `server_default=''`).
- `event_type` query parameter on `GET /api/v0/log-buffer` — filtering by event_type is no longer supported.
- `event_type` field from response body of `GET /api/v0/log-buffer` and `GET /api/v0/platform/logs` — both endpoints no longer include `event_type` in returned records.
- `event_type` kwarg from `new_root_log_event`, `new_downstream_log_event`, and `new_downstream_log_event_from_parent_id` helper functions.
- `engines/policy_assessment/enums.py` re-export shim removed (Phase 17 Step 6) — callers now import `PolicyType` and `AssessmentStrategy` directly from `inventory/policy/enums.py`.
- `engines/normalization/acl/` slice deleted entirely (Phase 17 Step 3) — runtime-broken normalizer (called deprecated `AccessArtifactService.upsert_artifact` / `AccessFactService.create_fact`); no live callers. Six dead `AccessFact*` stub error classes also removed from `inventory/access_facts/service.py`: `AccessFactNotRevokedError`, `AccessFactForeignKeyError`, `AccessFactActionSlugUnknownError`, `AccessFactApplicationScopeMismatchError`, `AccessFactNotActiveError`, `DuplicateActiveAccessFactError`.

### Changed

- `engines/ingest/service.py` — `connector.result.received` event payload now includes `now` (ISO-8601 UTC datetime); required by `application_sync.yaml` pipeline `args_from_payload` contract; previously missing field would cause JSON Schema validation failure on first real MQ delivery (Phase 18 Step 21 review)
- `engines/reconciliation/master_data_apply.py` — `_ACCEPTABLE_STATUSES` widened from `{pending_apply}` to `{pending_apply, applied, partially_applied}`; allows fan-out parallel `master_data_apply` steps (person/org_unit/employee) to complete without raising `ValueError` when a sibling step advances run status first (Phase 18 Step 21 review)
- `effective_access.project_application` action: `idempotent=False` → `idempotent=True` — service already implements UPSERT into `effective_grants` (safe to retry); flip mandated by ARCH_CONTEXT §355 (Phase 18 Step 21)
- TODO/FIXME comment hygiene pass (Phase 17 Step 23): 3 duplicate docstring TODO lines removed from `capability_grants/capability_projector.py`; 1 inline comment rewritten to `TODO(housekeeping-backlog):`; `runtime_settings/service.py` marker re-tagged from `TODO(tech-debt):` to `TODO(housekeeping-backlog):`. Comment-only diff; zero behaviour change.
- Enabled ruff `BLE001` (blind-except) rule across `aurelion-kernel/src/`. Every legitimate `except Exception` site now carries `# noqa: BLE001 # allowed-broad: <reason>` from a fixed 8-token vocabulary (`provider boundary`, `best-effort cleanup`, `task-loop guard`, `pipeline boundary`, `event handler swallow`, `best-effort log`, `test fixture cleanup`, `test orchestration`). New meta-test `src/platform/logs/tests/test_broad_except_discipline.py` enforces the companion comment and vocabulary. No behavioural change. (Phase 17 Step 21)
- Split `src/routers/v0.py` (118 LoC, 55 flat `include_router` calls) into three layer-scoped helper modules: `src/routers/_platform.py` (12 routers), `src/routers/_inventory.py` (31 routers), `src/routers/_engines.py` (12 routers). `src/routers/v0.py` is now a thin aggregator (~17 LoC) calling `include_platform_routers` / `include_inventory_routers` / `include_engine_routers` in order. No public REST surface change — every `/api/v0/...` path, method, and contract is byte-identical. Three external importers of the `router` symbol (`runtimes/platform_api/main.py`, `src/conftest.py`, `src/integration_tests/conftest.py`) continue to work unchanged. (Phase 17 Step 20)
- Extracted Iceberg / DuckDB / PyArrow physical I/O for `raw.access_artifacts` from `src/inventory/access_artifacts/service.py` into two new platform modules: `src/platform/lake/access_artifacts_writer.py` (upsert + tombstone) and `src/platform/lake/access_artifacts_reader.py` (single-row + cursor-paginated read). The inventory slice now keeps a thin domain-facing façade and re-exports moved error classes / result types for one phase. Public Python API of `AccessArtifactService` is byte-identical; no behaviour change. (Phase 17 Step 18)
- Extracted DuckDB read paths for `normalized.access_facts` (and the lake-side fragment of artifact-ref drill-down on `raw.access_artifacts`) from `src/inventory/access_facts/service.py` into a new platform module `src/platform/lake/access_facts_reader.py` with a frozen `AccessFactRow` dataclass and 5 public `run_*` entry points. The inventory slice now keeps a thin domain-facing façade. Public Python API of `AccessFactService` is byte-identical; no behaviour change. Reader-only extraction by design — the writer for `normalized.access_facts` lives in `engines/sync_apply/lake_writer.py` per the Reconciliation / Sync-Apply separation invariant (ARCH_CONTEXT 298–306) and is intentionally not extracted from inventory (inventory has not owned the access_facts write path since Phase 15 Step 16). Roadmap drift #6 acknowledged: Step 19 deviated from full mirror (a) to reader-only (O1) because writer is by design in engines/sync_apply per Reconciliation/Sync-Apply separation invariant. (Phase 17 Step 19)

### Added

- Runner parks pipeline runs on `wait_for_event` steps: `_parse_duration` helper (accepts `s`/`m`/`h`/`d` suffixes; rejects zero/invalid); `_park_wait_for_event` creates a `StepRun` + `PipelineEventWaiter`, flips step to `awaiting_event`, clears `worker_id`, flips run to `awaiting_event`, and returns `'awaiting_event'` sentinel so the worker slot is immediately freed. Fail-fast order: parse timeout → resolve match templates → DB writes. Invalid timeout or template failure calls `mark_pipeline_failed` with no DB rows created. `run_one_iteration` docstring updated to include `"awaiting_event"` outcome. 15 new tests (6 unit in `TestParseDuration`, 6 unit in `TestWaitForEventStep`, 1 integration in `TestWaitForEventIntegration`, unsupported-step-kind test rewritten). 168/168 orchestrator tests green; ruff + mypy clean. Resumption lands in Step 17. (Phase 18 Step 14)
- Orchestrator stale-run reclaim sweep: `reclaim_stale_run` + `list_stale_run_ids` in `PipelineOrchestratorService`
- `pipeline.run.heartbeat_lost` and `pipeline.step.aborted` event types
- `reclaim_sweep_tick` and `drain_active_run` coroutines + `RunHandle` dataclass in `runner.py`
- SIGTERM drain with `EXECUTOR_DRAIN_TIMEOUT_SECONDS` env var (Bootstrap tier) and `max(value, threshold+5)` clamp
- `PipelineOrchestratorService.refresh_heartbeat(run_id, worker_id) -> bool` (Phase 18 Step 12b): status-guarded UPDATE of `pipeline_runs.last_heartbeat_at`; the single documented exception to the event-emission invariant (liveness signal, not state transition). `_heartbeat_refresher` coroutine in `runner.py`: opens its own session per 3-second tick, survives `False`/exception, exits via `stop_event`. `run_one_iteration` now creates a refresher task around the action dispatch and awaits it in `finally`. 11 new tests (5 service, 4 refresher unit, 2 integration); 131/131 orchestrator tests green; ruff + mypy clean.
- `platform_executor_node` standalone runtime + `platform/orchestrator/runner.py` work loop (Phase 18 Step 12a). `WorkerIdentity` frozen dataclass (`<hostname>-<pid>-<slot_index>`). `run_one_iteration` two-session protocol: session A claims + commits; step_run committed before action; session B runs action; session C persists failure on rollback. `_resolve_templates` recursive walker supporting `${args.X}` (native type preserved on pure-reference match) and `${steps.<s>.result.<path>}` (dotted walk). `work_loop` polls at 1 Hz; respects `shutdown_event`. `PipelineOrchestratorService.claim_pending_run` added: `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1` + status-guarded UPDATE + `pipeline.run.started` emission. 22 new tests (5 claim, 10 runner unit incl. 8 `_resolve_templates`, 2 integration); 120/120 orchestrator + executor_node tests green; ruff + mypy clean on new files. Known limitation: SIGTERM during action waits for completion (drain + abort in Step 13).
- REST endpoints for the pipeline orchestrator: `GET /api/v0/pipelines`, `GET /api/v0/pipelines/{name}`, `GET /api/v0/pipeline-runs`, `GET /api/v0/pipeline-runs/{id}`, `GET /api/v0/pipeline-runs/{id}/steps/{step_name}`, `POST /api/v0/pipeline-runs` (201 fresh / 200 idempotent duplicate). Well-known discovery endpoints: `GET /api/v0/.well-known/pipeline-schema.json` (bundled grammar + additive per-action arg/result schemas under `$defs`) and `GET /api/v0/.well-known/pipeline-actions.json` (full `ACTION_REGISTRY` catalogue). Pipeline definitions served from `app.state.pipelines` (loader cache). Manual runs sit in `pending` until Step 12 (runner). No runner, no cancel/retry, no auth.
- Ingest engine emits `connector.result.received` after staging insert on inline/lake_ref paths (Phase 18 Step 10). Routing key equals `event_type`; payload: `{result_id, application_id, task_id}` (all UUID strings). `artifacts_bulk` branch is unaffected.
- `effective_access` read actions (`list_grants`, `explain_access`, `get_grant`) registered in `ACTION_REGISTRY`; all `idempotent=True`; read-only orchestrator surface — HTTP routes unchanged
- Engine actions for `policy_assessment.sod` (`evaluate`, `what_if`), `access_analysis.assessment_preview` (`detect_orphans`, `detect_terminated`), `access_analysis.capability_preview` (`resolve`), and `access_analysis.reports` (`deterministic`) registered in `ACTION_REGISTRY`; all `idempotent=True`
- `PipelineOrchestratorService` — sole writer for orchestrator tables with all status transitions, trigger-idempotency, cancel-vs-complete race handling, and heartbeat-lost reclaim
- `PipelineDefinitionLoader` in `platform/orchestrator/loader.py`: fail-fast YAML loader for `pipelines/*.yaml`. Validates structurally against `schema.json` (JSON Schema Draft 2020-12) and semantically via five checks — action-ref lookup against `ACTION_REGISTRY`, backward-only `requires` ordering, `${...}` templating ref resolution via BFS transitive closure, ≤1 schedule trigger per pipeline, and schedule-trigger-args ⊂ pipeline-args JSON Schema. Returns typed `PipelineDefinition` frozen dataclasses (9 fields, slots=True, sha256 `content_hash`). Six exception classes: `PipelineLoadError` base + `PipelineSchemaError`, `PipelineActionRefError`, `PipelineRequiresOrderError`, `PipelineTemplatingError`, `PipelineTriggerError`. Missing/empty directory returns `{}` without error. `jsonschema` promoted from dev group to main `dependencies`. `types-PyYAML` added to dev group. (Phase 18 Step 6)
- `ActionRegistry` foundation: `@register_action` decorator, `ActionContext` dataclass, `platform/orchestrator/registry.py`. In-memory singleton keyed by `(engine, action)`; `register`/`get`/`dispatch`/`all` methods; 5 custom exception classes (`ActionRegistryError`, `DuplicateActionError`, `ActionNotFoundError`, `ActionArgsValidationError`, `ActionResultValidationError`). Non-empty `engine`/`action` guard added per Guardian recommendation. No actions registered yet. (Phase 18 Step 5)
- Orchestrator storage foundation: `pipeline_runs`, `step_runs`, `pipeline_event_waiters` tables with four PG enum types and partial UNIQUE idempotency index (Alembic `a7e3b9d2f041`)
- `pipelines/schema.json` — JSON Schema Draft 2020-12 for pipeline YAML grammar (engine-call and wait-for-event step types; mq and schedule trigger types)
- `emit_safe` naming-discipline meta-test (`src/platform/logs/tests/test_emit_safe_discipline.py`): textual line-scan over `src/engines/**/service.py` + `src/inventory/**/service.py` requiring every `LogService.emit_safe(...)` call to carry an explicit `# allowed-emit-safe: <reason>` marker. ~45 existing call sites annotated with reasons from a fixed vocabulary (`observability`, `provider boundary`, `best-effort warning`). Zero behavioural change. (Phase 17 Step 17)
- Engines-wide layer-invariant guard test (`src/engines/tests/test_no_product_imports.py`): AST-scans `src/engines/**/*.py` for `from src.products` / `import src.products` and fails on any hit. Zero hits today; guard fires the day a `src/products/` layer is introduced (Phase 17 Step 16).
- Test coverage backfill (Phase 17 Step 15): cartridge DSL edge cases (`greater_than` against string-ish numbers and `None`; deeply nested mixed `all`/`any` trees, 4 levels) and an explicit double-apply idempotency assertion at the Iceberg level for sync-apply (ARCH_CONTEXT line 292 invariant: "Idempotency MUST be enforced at the Iceberg level, not just via PG status"). Zero production code changes.
- Reconciliation observability backfill (Phase 17 Step 14): `LogService.emit_safe()` WARNING calls at two pipeline skip branches — handler exception per artifact and unknown action slug per candidate. New `RuntimeSettingsConfig.reconciliation_fetch_batch_size` (default 5000, range 1–50000) wired through `LakeSettings` and propagated to `run_reconciliation`; operator-tunable without redeploy via the existing `runtime_settings` table. Note: advisory lock + entry/exit/failed logs were already shipped in Phase 15 Step 9.
- `GET /api/v0/policies/catalog` — product-neutral, read-only unified policy catalog. One row per policy across two sources: DB-backed SoD rules (`sod_rules`) and file-backed Lens cartridges (`cartridges/lens/**/*.yaml`). Each row carries the three policy axes (`policy_type`, `definition_source`, `assessment_strategy`) plus `status` (`active`/`inactive` for SoD; `available` for cartridges) and optional cartridge `version`. Read-only — no events, no mutation, does not change scan or assessment behaviour.
- `AssessmentStrategy` enum extended with `heuristic` and `hybrid` to match the catalog axis vocabulary; existing `deterministic` and `semantic_assisted` values are unchanged. New enums `DefinitionSource` (`db`/`file`) and `PolicyStatus` (`active`/`inactive`/`available`) introduced for the catalog projection.

### Fixed

- `SyncApplyService.apply` now correctly raises `SyncApplyAlreadyExecutedError` (HTTP 409) when an apply run for the same `reconciliation_run_id` is already in `running`, `completed`, or `partially_applied` status. Previously only `running` was treated as blocking, which contradicted the documented service-level idempotency contract and the API reference (`reference/reconciliation.md`). Operator-visible: clients that re-invoked apply against an already-completed reconciliation run received a successful re-apply instead of `409`, which could lead to double-apply against the lake. Fix is in `engines/sync_apply/repository.py::find_active_apply_run` — `active_statuses` now includes `completed` and `partially_applied`. Discovered via integration test `test_lake_only_pipeline` Stage 6 after Phase 17 Step 11 incorrectly retired `test_apply_twice_raises_already_executed` as a docstring-vs-test conflict; the deleted unit test was in fact a valid regression guard for this bug, and Stage 6 of the integration suite now covers the same surface.

- `AccessArtifactService.list_artifacts_iceberg`: removed unsupported `skip_schema_inference=true` option from the `iceberg_scan(...)` call — DuckDB 1.5.2 does not accept this parameter and raised at runtime on every list/get-by-id request hitting the Iceberg read path. Production read path was effectively broken; F4 tests masked the regression because they never exercised this code.
- `AccessArtifactService.list_artifacts_iceberg`: replaced `lake_session.fetchmany(1)` with `lake_session.fetchone()` — `LakeSession` exposes no `fetchmany` method, so the previous call raised `AttributeError` before returning any row.

### Changed

- `AccessArtifactService.__init__` now accepts optional `event_service: EventService | None = None` (defaults to `noop_event_service`) — aligns with the inventory sibling-service convention. Plumbing only: no `emit(...)` sites are added in this slice. All existing kwargs-only call sites are unaffected.

### Fixed

- `access_analysis` engine: `iter_unused_access_fact_views` now skips rows with `subject_id IS NULL` instead of attempting to parse them and crashing the whole scan with `ValueError: badly formed hexadecimal UUID string`. Iceberg schema declares `subject_id` as `required=False`, so orphan-fact rows (account present, subject not matched) can legitimately appear; they are out of scope for the unused-access detector (covered by `orphan_access`). Symmetric to the existing null-`application_id_denorm` filter; emits a DEBUG log per skipped row.

- `GET /api/v0/analytics/top-risks` and `GET /api/v0/analytics/risk-by-application`: `severity_breakdown` now includes the `informational` key. Previously the dict had only four keys while `open_findings_count` summed all five severities — `sum(severity_breakdown.values())` could be less than `open_findings_count` when informational findings existed. Both endpoints now return all five `SodSeverity` keys, zero-filled for absent severities. `informational` is counted but contributes 0 to `risk_score`. Aligns the shape with `findings-summary.findings_by_severity`.

### Added

- `GET /api/v0/reports/deterministic` — product-neutral deterministic report payload combining `FindingsSummary`, top findings with evidence, rule-based recommendations, and five fixed executive summary blocks. No LLM, no PDF — JSON only. Designed as input for Lens AI summary (Phase 16 Step 22) and any future renderer.

- `GET /api/v0/analytics/findings-summary` — counts open findings, breaks them down by severity and kind, and lists top applications, top subjects, and quick-win candidates. Pure PG; no DuckDB or Iceberg dependency.

- `POST /api/v0/accounts/bulk` — bulk upsert accounts by `(application_id, username)`. Inserts new rows or updates `display_name` and `email` on conflict. Returns `{upserted: N}`. Accepts 1–10000 items.
- Unique index `ix_ent_accounts_app_username` on `ent_accounts(application_id, username)` (migration `cf1a266d2661`). Enables idempotent bulk upsert via `INSERT ON CONFLICT DO UPDATE`.

- `GET /api/v0/org-units` — returns all org units (id, external_id, name) sorted by external_id ascending. No pagination, no filtering. Intended for Lens lookup use.
- `POST /api/v0/org-units/bulk` — upsert org_units by `external_id` with two-pass parent resolution (supports child-before-parent CSV ordering). Returns `{upserted, ids}`. Emits `inventory.org_unit.bulk_upserted`. 422 on unknown `parent_external_id`.
- Extended `POST /api/v0/employees/bulk` — optional `org_unit_external_id` field links employees to org units; returns 422 if referenced org_unit not found. Resolves via one batched `SELECT ... WHERE external_id IN (...)`.
- Migration `2026_05_02_2000_phase_16_step_15_org_units`: creates `org_units` table + adds `employees.org_unit_id` nullable FK column.

- `POST /api/v0/subjects/bulk` — bulk upsert employee-kind subjects by `(kind, external_id)`, resolving `person_external_id → employee_id`
- `UNIQUE` constraint `uq_subjects_kind_external_id` on `(subjects.kind, subjects.external_id)` (migration `2026_05_02_1620`)

- `POST /api/v0/employees/bulk` — bulk upsert employees by `person_external_id` (idempotent, up to 500 items).
  Request: `{items: [{person_external_id, is_locked, description}]}`.
  Response: `{upserted: N, ids: [...]}` in input order.
  Returns 422 if any `person_external_id` is unknown, or if duplicates appear within a single payload.
  Event: `inventory.employee.bulk_upserted` with `count` and `person_ids`.
- `UNIQUE` constraint `uq_employees_person_id` on `employees.person_id` (migration `9555ec2e84da`).
  Pre-flight: verify no duplicate `person_id` rows before applying in production.

- `POST /api/v0/persons/bulk` — bulk upsert persons by `external_id` (idempotent, up to 500 items)
- `UNIQUE` constraint `uq_persons_external_id` on `persons.external_id` (migration `9372cefb0a63`)

- Analytics slice with `GET /api/v0/analytics/top-risks?limit=N` and `GET /api/v0/analytics/risk-by-application` — DuckDB over Iceberg `normalized.access_facts` joined with PG `findings`
- `GET /api/v0/access-facts/{fact_id}/artifact-ref` resolver — closes drill-down chain `access_fact → reconciliation_delta_item → access_artifact`; 404 if any link is broken
- `AccessFactArtifactRefRead` schema and `AccessFactArtifactRefNotFoundError` in access_facts slice
- Named severity weight constants (`critical=100`, `high=50`, `medium=20`, `low=5`) in analytics schemas
- Test bootstrap fix: secrets provider registration and NHI/subjects model imports in conftest

- `src/platform/runtime_settings/` slice with `GET /api/v0/runtime-settings`, `GET /api/v0/runtime-settings/{key}`, and `PUT /api/v0/runtime-settings/{key}` endpoints
- `RuntimeSettingsConfig` typed snapshot with operational knobs for lake, LLM, and log buffer
- `src/core/config/` bootstrap layer — `get_settings()` with `lru_cache`, `PostgresSettings`, `RabbitMQSettings`, `AppSettings`, `LakeStaticSettings`
- `src/core/secrets/` — `ConfigSecretManager` Protocol for bootstrap config layer
- `.secrets.json.example` template for `AURELION_SECRETS_FILE`
- Alembic migration `2026_04_28_0553_add_runtime_settings` — additive `runtime_settings` table

### Changed

- `src/core/db/session.py` — lazy `get_engine()` / `get_session_factory()` replacing module-level singletons
- `src/platform/lake/config.py` — `LakeSettings(BaseModel)` with `build_lake_settings()` factory
- `.env` reduced to two bootstrap vars (`AURELION_SECRET_PROVIDER`, `AURELION_SECRETS_FILE`)
- All runtime entrypoints migrated to `get_settings()`; `load_dotenv()` moved above all `src.*` imports
- LLM knobs moved from `LLMSettings` singleton to `RuntimeSettingsConfig`

### Removed

- `Settings(BaseSettings)` singleton and `settings = Settings()` module-level instance
- `LLMSettings(BaseSettings)` singleton from `src/platform/llm/`
- Module-level `engine` and `SessionLocal` from `src/core/db/session.py`

### Security

- `PUT /api/v0/runtime-settings/{key}` has no AuthN/AuthZ in this release — gate at reverse proxy / service mesh in non-dev deployments

### Notes

- Phase 18 — Native Pipeline Orchestrator — closed 2026-05-11 (Step 27). Roadmap: `aurelion-mas/roadmap/phase_18.md`. User docs: `aurelion-docs/docs/concepts/pipeline-orchestrator.md`.

## [0.1.7] - 2026-04-27

### Added

- Phase 15 Data Lake Migration complete (20/20 milestones)
- `src/platform/lake/` slice — Iceberg catalog, DuckDB session factory, table schemas, provisioning, maintenance
- `capabilities/sync_apply/` slice with `SyncApplyService`, `lake_writer`, crash-recovery preflight, and `SyncApplyRun`/`SyncApplyResult` ORM models
- `capabilities/lake_migration/` slice — resumable PG → Iceberg migration with synthetic delta provenance
- `capabilities/reconciliation/hashing.py` — `compute_natural_key_hash` shared helper
- `GET /api/v0/lake/status` — catalog URI, warehouse URI, storage provider, per-table snapshot metadata
- `POST /api/v0/lake/compaction` — `compact_table` + `expire_old_snapshots` + `clean_orphan_files` with active-write safety gate
- `GET /api/v0/datalake/batches` — keyset-paginated lake batch listing, `snapshot_id` serialized as string
- `POST /api/v0/lake-migrations` / `GET /api/v0/lake-migrations/{id}` / `GET /api/v0/lake-migrations` — migration run lifecycle
- `POST /api/v0/reconciliation/runs/{run_id}/apply` — sync/apply with modes `auto_apply`, `manual_apply`, `selected_items`, `dry_run`
- `GET /api/v0/reconciliation/runs/{id}` and `GET /api/v0/reconciliation/runs/{id}/delta-items` — delta inspection endpoints
- `ReconciliationDeltaItem` and `ReconciliationRun` ORM models; `SyncApplyRun` and `SyncApplyResult` ORM models
- `inventory.access_fact.{created,updated,revoked,reactivated}` events emitted exclusively from `sync_apply/service.py`
- Four reconciliation domain events: `reconciliation.run.started`, `reconciliation.delta.created`, `reconciliation.run.completed`, `reconciliation.run.failed`
- `AccessArtifactView` and `AccessFactView` frozen Pydantic v2 DTOs replacing deleted ORM models
- E2e integration test suite (`test_phase15_e2e_lake_only_pipeline`) and env-gated parity test (`test_phase15_parity_matches_golden_fixture`)
- `src/integration_tests/fixtures/phase15_dataset.json` — 50-artifact curated parity fixture

### Changed

- `access_artifacts` and `access_facts` PG tables dropped (Alembic `i3j4k5l6m7n8`); Iceberg is sole source of truth
- FK `artifact_bindings.artifact_id → access_artifacts.id` dropped (Alembic `h2i3j4k5l6m7`); soft Iceberg references enforced at service layer
- FK constraints from `effective_grants`, `access_usage_facts`, `initiatives` to `access_facts.id` dropped (Alembic `j4k5l6m7n8o9`)
- `ReconciliationRun.application_id` nullable for cross-app migration runs
- `AccessArtifactService` and `AccessFactService` rewritten to lake-only (DuckDB/Iceberg)
- `unused` access-analysis detector migrated to DuckDB `iceberg_scan` on `normalized.access_facts`
- `LakeSettings.artifacts_write_backend` default flipped to `'iceberg'`
- Reconciliation pipeline reads from Iceberg; `_phase_apply_delta` replaced by `_phase_persist_delta`
- PG advisory lock per `application_id` on reconciliation runs (HTTP 409 on contention)
- Lake operational endpoints and `al lake` CLI commands documented in `docs/reference/data-lake.md`

### Removed

- `inventory/access_artifacts/models.py`, `repository.py` — lake-only DTOs replace ORM
- `inventory/access_facts/models.py`, `repository.py` — lake-only DTOs replace ORM
- `AccessArtifactNotFoundError`; ORM-backed service/repository/model tests

### BREAKING

- Reconciliation `Handler.handle` now takes `AccessArtifactView` DTO instead of `AccessArtifact` ORM class
- `AccessFactService.create_fact / revoke_fact / refresh_fact_fields` now require mandatory `delta_item_id: UUID` argument
- `AccessArtifactService.upsert_batch` and `tombstone_batch` no longer accept `session: AsyncSession`
- `snapshot_id` on `LakeBatchRead` responses serialized as JSON string (was integer)

## [0.1.6] - 2026-04-26

### Added

- `CorrelationIdMiddleware` with `X-Correlation-ID` echo/generate on every HTTP request
- `core.context` module with `correlation_id_var` ContextVar and `current_correlation_id()` accessor
- `new_event_envelope` builder in `platform/events` with ContextVar `correlation_id` fallback
- `new_root_log_event` ContextVar fallback before UUID generation
- `LLMModel` entity with `LLMProvider` enum (`llama_cpp`, `openai`, `ollama`) and `llm_models` table
- Alembic migration `phase_14_step_02_llm_models` — `llm_provider` PG enum + `llm_models` table
- `LLMExecutionProfile` entity with `param_overrides` JSONB and FK to `LLMModel`
- Alembic migration `phase_14_step_03_llm_execution_profiles` — `llm_execution_profiles` table
- `AbstractLLMProvider` ABC with `LLMMessage`, `LLMChunk`, and `LLMRole` types in `platform/llm/providers/base.py`
- `LlamaCppProvider` with async-generator `stream()`, cooperative `abort()`, and `LlamaCppProviderError` hierarchy
- Optional dependency extra `llm-llama-cpp` pulling `llama-cpp-python>=0.3.0`; kernel imports cleanly without it
- Phase 14 Step 6 — `LLMFactory` in `src/platform/llm/factory.py`: in-process LRU registry keyed by `LLMModel.id`, configurable `max_loaded_models` (default 2), per-`model_id` async load lock to coalesce concurrent loads, eviction awaits `provider.abort()`, `invalidate(model_id)` and `invalidate_all()` with race-safe in-flight-load discard; `LLMFactoryError` hierarchy (`LLMModelNotFoundError`, `LLMModelInactiveError`, `LLMProviderNotSupportedError`); `llama_cpp` branch only — `openai`/`ollama` deferred
- `LLMSettings` (pydantic-settings v2) with `LLM_MAX_LOADED_MODELS`, `LLM_MAX_MESSAGES`, `LLM_MAX_CHARS_PER_MESSAGE`, `LLM_MAX_TOTAL_CHARS` operator knobs; `LLMFactory` resolves `max_loaded_models` from settings when not passed explicitly
- `GET /api/v0/llm/models`, `POST /api/v0/llm/models`, `GET /api/v0/llm/models/{id}`, `PATCH /api/v0/llm/models/{id}`, `DELETE /api/v0/llm/models/{id}` — LLMModel CRUD; provider-wiring + token-limit + path-readability validation; `LLMFactory` cache invalidation post-flush on deactivate/path/url/ref changes; exception → HTTP mapping: `LLMModelNotFoundError` → 404, `LLMModelNameAlreadyExistsError` → 409, `LLMModelInvalidConfigError` → 422
- `GET /api/v0/llm/execution-profiles`, `POST /api/v0/llm/execution-profiles`, `GET /api/v0/llm/execution-profiles/{id}`, `PATCH /api/v0/llm/execution-profiles/{id}`, `DELETE /api/v0/llm/execution-profiles/{id}` — LLMExecutionProfile CRUD; `model_id` immutable on PATCH (absent from `LLMExecutionProfileUpdate`); `param_overrides` replace semantics; exception → HTTP mapping: `LLMProfileNotFoundError` → 404, `LLMProfileNameAlreadyExistsError` → 409, `LLMProfileInvalidConfigError` → 422
- `LLMExecutionProfileUpdate` schema (`extra='forbid'`, `model_id` excluded, both fields Optional)
- `LLMProfileNotFoundError`, `LLMProfileNameAlreadyExistsError`, `LLMProfileInvalidConfigError` domain exceptions for profile CRUD
- Repository helpers `get_profile_by_id`, `get_profile_by_name`, `list_profiles` in `src/platform/llm/repository.py`
- `POST /api/v0/inference` — JSON inference endpoint: resolves `execution_profile_id`, validates messages against `LLMSettings`, drives `LLMFactory` provider, returns `InferenceResponse` with `output`, `tokens_used`, `latency_ms`, `ttft_ms`, `model_id`, `execution_profile_id`; logs every call (success / error / aborted) to `aurelion.logs`
- `POST /api/v0/inference/stream` — SSE inference endpoint: streams token events (`{"token": ..., "done": false}`) then final event (`{"output": ..., "done": true}`); client disconnect triggers `provider.abort()` and an aborted log entry
- `LLMInferenceValidationError` domain exception for message size limit violations; `LLMMessageIn`, `InferenceRequest`, `InferenceResponse` Pydantic schemas; `sse-starlette>=1.8.2` added as runtime dependency
- `POST /sod-rules/apply` — idempotent config-as-code upsert for SoD rules; capabilities referenced by slug; full condition sync (create/replace/delete) in one transaction; returns diff summary (`rules_created`, `rules_updated`, `rules_unchanged`, `conditions_created`, `conditions_deleted`); 422 on unknown capability slugs
- `apply_service.py` in `sod_rules/` slice — pure upsert logic keyed by rule `code` and condition `name`
- `SodApplyPayload`, `SodConditionSpec`, `SodRuleSpec`, `SodApplyResult` schemas in `sod_rules/schemas.py`
- Development seed script `scripts/seed_dev.py` — Meridian Fintech scenario (2 apps, 3 employees, 1 NHI, 6 accounts, 5 capabilities, 5 mappings, 2 SoD rules, 6 grants)

### Fixed

- `POST /scan-runs`, `PATCH /scan-runs/{id}/status`, `POST /scan-runs/{id}/run` routes missing `await session.commit()` — scan run rows were created in-memory but never persisted to the database
- `GET /capability-grants` without any filter now returns `400 Bad Request` — at least one of `subject_id`, `capability_id`, `application_id`, `source_effective_grant_id`, or `source_capability_mapping_id` is required
- `LLMModelUpdate` with `name=null` now returns 422 instead of 500 — NOT NULL guard in service before flush
- `_translate_integrity_error` now distinguishes FK violations by `constraint_name`: dependent profiles vs bad `secret_id` vs unknown FK (re-raised)
- `_translate_profile_integrity_error` constraint name corrected to match actual DB constraint for profile-model FK

### Added

- Phase 14 LLM Platform Layer complete (13/13 milestones)
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

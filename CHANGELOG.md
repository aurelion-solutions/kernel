# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

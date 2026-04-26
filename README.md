# aurelion-kernel

**The core API server and platform layer of Aurelion** — a foundational identity and access platform for building enterprise-grade governance and security solutions.

This repository is the heart of the system: REST API, database models, message queue integration, and all platform services (inventory, capabilities, LLM, secrets, events, logs).

---

## What Is Aurelion

Aurelion is not a single product. It is an **identity fabric** — a system that provides the underlying data, control, and execution layers upon which multiple products can be built.

Three logical layers:

| Layer | What it does |
|---|---|
| **Platform** | Event-driven infrastructure, audit streams, MQ pipelines, shared services (LLM, secrets, logs) |
| **Inventory** | Source of truth for identities — subjects, accounts, access facts, effective access, ownership |
| **Products** | IGA, IDP, ITDR, NHI security, CIAM — built on top of Platform + Inventory |

All critical logic (access resolution, SoD, analytics) is **deterministic and auditable**. AI is used only for interpretation and suggestions — never for core decisions.

---

## This Repository

`aurelion-kernel` contains:

- **REST API** — FastAPI, port 8000
- **Database models** — SQLAlchemy ORM, PostgreSQL 17
- **Message queue** — RabbitMQ 4.2 integration
- **Platform services** — storages, secrets, events, logs, LLM (models, execution profiles, inference)
- **Capability engines** — SoD analysis, access projections, findings, mitigations
- **Inventory** — subjects, employees, NHI, accounts, resources

---

## Getting Started

**Prerequisites:** Python 3.13+, [uv](https://docs.astral.sh/uv/), Docker (for PostgreSQL + RabbitMQ)

```bash
# Start infrastructure
docker compose up -d

# Install dependencies
uv sync

# Run migrations
uv run alembic upgrade head

# Start the API server
uv run uvicorn src.runtimes.platform_api.main:app --reload --log-level debug --access-log
```

API available at `http://localhost:8000`. Docs at `http://localhost:8000/docs`.

---

## Development

```bash
# Run all tests
uv run pytest

# Lint + format (must pass before every commit)
uv run ruff check . --fix
uv run ruff format .

# Type check
uv run mypy

# Create a new migration
uv run alembic revision --autogenerate -m "describe the change"
```

Test configuration: `asyncio_mode="auto"`, `--import-mode=importlib`.

---

## Architecture

Dependencies flow **downward only**:

```
Products (IGA, IDP, ITDR)
    ↓
Capability Engines (SoD, projections, ingest)
    ↓
Platform / Kernel (DB, MQ, LLM, secrets, events, storages, datalake)
```

Every slice follows the same structure:

```
src/<layer>/<slice>/
├── models.py      # SQLAlchemy ORM
├── schemas.py     # Pydantic v2
├── service.py     # Business logic + event emission
├── routes.py      # Thin handlers
└── tests/
```

All routes are registered under `/api/v0/`. Events flow through `aurelion.events` (domain/audit) and `aurelion.logs` (operational) — two separate buses.

---

## License

[BUSL-1.1](LICENSE) — source-available.
Allows inspection and internal use; protects against redistribution in competing products.

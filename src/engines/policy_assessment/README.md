<!--
SPDX-FileCopyrightText: 2026 Michael Abramovich

SPDX-License-Identifier: BUSL-1.1
-->

# policy_assessment engine — ownership rules

## policy_types/

Domain-specific policy evaluators. Each subdirectory owns one policy type's
evaluation logic end-to-end (pure functions + service + DB queries).

Example: `policy_types/sod` — Segregation of Duties.

## strategies/

Evaluation strategies — how evidence is gathered and decisions are reached.
A strategy is independent of any specific policy type.

Example: `strategies/deterministic` — YAML rule-pack evaluation.
Example: `strategies/semantic_assisted` — semantic evidence extraction.

## Source formats vs strategies

YAML, JSON, and DB rows are policy **definition source formats**, not strategies.
`strategies/deterministic` happens to load YAML rule-packs, but "deterministic"
describes the evaluation contract, not the storage format.

## semantic_assisted is not an LLM module

`strategies/semantic_assisted` owns how semantic evidence is **used during
assessment**. It does not own providers, clients, embeddings, or RAG
infrastructure — those belong to `platform/llm`.

## Output contracts

`policy_assessment` returns `Decision` and/or `PolicyAssessmentOutput`.

It does **not** own:
- persistent policy definitions (`inventory/policy`)
- capabilities, grants, or mappings (`inventory/access_model`)
- findings, mitigations, or feedback (`inventory/assessment`)

## Persistent ownership map

| What | Owner |
|------|-------|
| Policy definitions (SoD rules, conditions) | `inventory/policy` |
| Capabilities, grants, mappings | `inventory/access_model` |
| Scan results, findings, mitigations | `inventory/assessment` |
| LLM providers, RAG, embeddings | `platform/llm` |

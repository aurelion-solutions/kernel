<!--
SPDX-FileCopyrightText: 2026 Michael Abramovich

SPDX-License-Identifier: BUSL-1.1
-->

# access_orchestration engine

## What this is

`access_orchestration` is the live orchestration engine for intended access
operations. It processes explicit intents to change or validate access state and
coordinates the downstream engines needed to fulfil them.

## Not the same as access_analysis

| | access_analysis | access_orchestration |
|---|---|---|
| Mode | Retrospective / batch | Live / intent-driven |
| Trigger | Scan run | Explicit intent (request, event, action) |
| Input | Existing access state | Intent to change/validate access |
| Output | Findings, violations | Decision + next step |

`access_analysis` asks: *"what is wrong with access that already exists?"*

`access_orchestration` asks: *"should this access change happen, and what needs to run?"*

## Sources of intent

An `AccessOrchestrationIntent` can originate from any of:

- Employee self-service access request
- Joiners / Movers / Leavers (JML) lifecycle event
- Manager or admin direct action
- Remediation of an existing finding
- SoD mitigation action
- API or import-driven operation

## Delegation model

`access_orchestration` does not own the logic it delegates to:

| Concern | Owner |
|---|---|
| Policy evaluation | `engines/policy_assessment` |
| Current effective access state | `engines/effective_access` |
| Execution against target systems | `engines/provisioning` |
| Human approval flow | workflow/BPM (future) |
| Findings / audit results | `inventory/assessment` |

## What this engine owns

- Interpreting the intent and deciding the next step
- Sequencing calls to policy_assessment, effective_access, provisioning
- Returning a structured `AccessOrchestrationResult` to the caller

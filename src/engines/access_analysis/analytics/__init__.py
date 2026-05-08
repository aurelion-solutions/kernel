# SPDX-FileCopyrightText: 2026 Michael Abramovich
#
# SPDX-License-Identifier: BUSL-1.1

"""Analytics slice — read-only DuckDB/Iceberg analytics over access_facts + findings.

Pre-check result: Outcome B (drill-down chain broken).
- AccessFactRead does NOT expose reconciliation_delta_item_id.
- Iceberg normalized.access_facts has NO source_artifact_id column.
- The only bridge is via PG reconciliation_delta_items.source_artifact_id.
- Resolver endpoint GET /access-facts/{fact_id}/artifact-ref added to
  inventory/access_facts/ slice to close the chain.
"""

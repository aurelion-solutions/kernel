# pipelines/

Pipeline YAML definitions for the native orchestrator (Phase 18+).

`schema.json` is the single source of truth for the pipeline grammar (JSON Schema Draft 2020-12).
All loaders, validators, and IDE tooling reference this file — no parallel schema definitions elsewhere.
Forbidden locations for pipeline YAMLs: engine slices, `cartridges/`, `products/`.

## How the loader uses this directory

`PipelineDefinitionLoader` (in `src/platform/orchestrator/loader.py`) is called once per process to
load all `*.yaml` files from this directory into typed `PipelineDefinition` objects.  The loader is
fail-fast: the first structural or semantic violation aborts the process with a descriptive exception.

Validation order:
1. Structural check against `schema.json` (JSON Schema Draft 2020-12).
2. Action reference check — every `engine`+`action` pair must be registered in `ACTION_REGISTRY`.
3. `requires` backward-only check — no forward or unknown step references.
4. Templating check — every `${...}` expression must resolve to a declared pipeline arg or a step
   in the transitive `requires` closure.
5. Trigger check — at most one `schedule` trigger; schedule `args` must be a subset of
   `pipeline.args.properties`.

`schema.json` is excluded from the `*.yaml` glob and is never loaded as a pipeline.

## Templating syntax

Template expressions use `${...}` with two legal forms:

- `${args.X}` — substituted with the pipeline-level argument `X` at runtime.
- `${steps.<name>.result.<path>}` — substituted with a field from a prior step's result.

**`${...}` is always treated as a live reference.**
`$$` escape syntax is **not** supported — the loader and the Step 12 runtime substitutor both treat
every literal `${...}` as a ref.  To include a literal dollar-brace sequence in a value, do not use
template syntax at all.

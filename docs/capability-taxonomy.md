# Case taxonomy and authoring

Each case lives once under `capabilities/<capability>/cases.jsonl`. Source, model,
regression and transport are metadata, not duplicate trees. Use
`kimi-harness plan --select all --format summary` for the current inventory.

| Capability area | Boundary to inspect |
|---|---|
| `protocol` | API validation / request normalization |
| `tool_call/choice` | tool context / renderer / grammar selection |
| `tool_call/declaration` | message parser / renderer / tool registry |
| Tool arguments and schemas | detokenizer / tool parser / schema conversion / grammar compiler |
| `structured_output` | response grammar / grammar composition |
| `reasoning` | request normalization / renderer / reasoning parser |
| `streaming` | stream assembler / response serializer |
| `prompt_rendering` | tokenizer / checkpoint encoder / renderer |
| `usage` | token accounting / response serializer |
| `conversation_state` | history normalization / id allocator |
| `multimodal` (knowledge only) | multimodal processor / renderer |
| `error_handling` | validation / frontend / service boundary |

These are search hints, not diagnoses or claims that every area has endpoint
coverage. The [knowledge index](../knowledge/README.md) links domain guidance.

## Case contract

[`capability-case.schema.json`](../schemas/capability-case.schema.json) defines
source cases and the shared portion of frozen-plan cases. Required metadata:
stable id/title/capability, tags, source and adaptation, knowledge links, likely
components, determinism, role/stage, wire modes, output cap and request steps.

- Roles: `gate`, `diagnostic`, `observation`, `platform`. Observation role and
  observation determinism must agree; role decides whether a pass is permitted.
- Stages: `protocol`, `tooling`, `conversation`, `robustness`, `regression`.
- `requires` adds target requirements; applicability also derives from successful
  requests. Profiles describe obligations, not implementation progress. See
  [target contracts and gates](framework-bringup.md).
- Step-level `thinking`/`effort` is canonical intent translated for the access
  dialect. Enabled canonical thinking requires an explicit verified effort; no
  guessed max/default is inserted. Raw alias/conflict/default probes omit
  canonical fields and retain wire bytes.
- Valid completed tool calls always require unique ids, declared names and
  nonempty JSON-object arguments. There are no per-case syntax opt-out flags.
  `arguments_schema` adds schema constraints; `first_call_only` limits only the
  case-specific schema/history checks, not these shared invariants.
- Tag long-history and platform-specific risky probes `opt_in`; never silently
  add them to smoke. Requests, source and expected values must be secret-free.

Keep endpoint cases, parser/renderer component vectors and harness pytest tests
separate. Their evidence is not interchangeable. Use the
[agent protocol](agent-use.md) for execution/result contracts and
[bring-up guide](framework-bringup.md) for adapters and response binding.

## Add a regression

1. Inspect a related case using `plan --select <id> --format json`; summary/catalog
   expose local file:line, components, knowledge and source without long requests.
2. Copy [case-template.json](../examples/case-template.json), choose a unique id,
   replace its source/adaptation and assertions, then append **one compact JSON
   object per line** under the matching capability directory. The template is not
   collected or executed from `examples/`.
3. Run `kimi-harness lint --file capabilities/<capability>/cases.jsonl --id <id>`.
   Multiple files/ids are supported. Diagnostics carry `[file, line, ...JSON path]`.
   Malformed JSON in any scanned file still fails closed because its id is unknown.
4. Run full `kimi-harness lint` before sharing, then the relevant offline negative
   tests. Focused lint validates only its declared scope; endpoint plans continue
   to validate the entire catalog and selected fixture/step dependencies.
5. Generate a new target-bound JSON plan, review budget/opt-in and get authorization
   before endpoint execution. Request/expect contradictions are author errors,
   not engine failures. Never change `tool_choice` expectations to bypass checks.

Minimize the incident, add one capability case with pinned source/adaptation,
then add an offline negative oracle test. Record symptom → suspected boundary →
confirmed cause → fix → versioned evidence in the linked knowledge page. Mark
confidence (`contract`, `format`, `implementation_choice`, `historical`,
`unverified`); do not weaken the oracle or import a benchmark score as a pass.
Source-extraction boundaries are in [migration.md](migration.md).

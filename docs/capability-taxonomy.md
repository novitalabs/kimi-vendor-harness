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

- Roles: `gate`, `diagnostic`, `observation`, `platform`.
- Stages: `protocol`, `tooling`, `conversation`, `robustness`, `regression`.
- `requires` adds target requirements; applicability also derives from successful
  requests. Profiles describe obligations, not implementation progress. See
  [target contracts and gates](framework-bringup.md).
- Step-level `thinking`/`effort` is canonical intent translated for the access
  dialect. Raw alias/conflict probes omit canonical fields and retain wire bytes.
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

Minimize the incident, add one capability case with pinned source/adaptation,
then add an offline negative oracle test. Record symptom → suspected boundary →
confirmed cause → fix → versioned evidence in the linked knowledge page. Mark
confidence (`contract`, `format`, `implementation_choice`, `historical`,
`unverified`); do not weaken the oracle or import a benchmark score as a pass.
Source-extraction boundaries are in [migration.md](migration.md).

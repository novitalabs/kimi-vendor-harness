# Capability taxonomy

This repository maintains Kimi agentic **engine behavior**, not model-quality
benchmarks or formal vendor acceptance. Passing cases do not establish planning,
reasoning, coding, vision or long-context benchmark scores.

## Organization

A case has one primary capability and cross-cutting metadata. It lives once
under `capabilities/<capability>/`; source, model, regression and transport are
metadata, not duplicate directory trees. Endpoint case definitions use JSONL;
the parser vector contract is a separate JSONL file in the prompt-rendering area.

| Primary capability | What it observes | Likely implementation boundary |
|---|---|---|
| `protocol` | request fields, token limits, validation/defaults | API validation / request normalization |
| `tool_call/choice` | none/auto/required/named behavior | tool context / renderer / grammar selection |
| `tool_call/declaration` | global/dynamic tools, names and schemas | message parser / renderer / tool registry |
| `tool_call/arguments` | JSON arguments, marker data and bad calls | detokenizer / tool parser / response assembly |
| `tool_call/schema` | constrained arguments, refs and invalid schemas | schema conversion / grammar compiler |
| `structured_output` | json_object/json_schema and tools combinations | response grammar / grammar composition |
| `reasoning` | thinking switches, effort and channel boundaries | request normalization / renderer / reasoning parser |
| `streaming` | raw SSE, tool deltas, finish and usage | stream assembler / response serializer |
| `prompt_rendering` | literal markers, history and token parity | tokenizer / checkpoint encoder / renderer |
| `usage` | counts/details and stream/nonstream consistency | token accounting / response serializer |
| `conversation_state` | tool-result association and continuation | history normalization / id allocator |
| `multimodal` | image parts and token fixtures, not vision quality | multimodal processor / renderer |
| `error_handling` | status/error envelope and transport separation | validation / frontend / service boundary |

## Execution layers

- **endpoint**: raw HTTP request and externally observable response;
- **parser**: vectors sent to a framework-supplied adapter; no inference traffic;
- **local**: harness/client invariants checked with fake responses.

Endpoint tests cannot force a chosen model token sequence. Parser vectors cannot
prove endpoint generation. Prompt-token equality cannot prove tool-result ordering:
reordered segments can have the same token count.

## Metadata and plan

Each endpoint case declares a stable id, title, capability, tags, source path and
adaptation, knowledge links, likely components, determinism, modes, output cap
and steps. `kimi-harness plan` reports request count and output-token budget;
`kimi-harness run` requires `--execute`, a request ceiling and has no implicit
retries or official repeat waves. `doctor` and `plan` are zero traffic.
`plan --format summary` lists selected cases; `plan --select all` explicitly
inspects the full catalog. `parser validate` and `parser run` are separate
offline commands under the same `kimi-harness` CLI. For agent invocation,
`plan --service-access ... --out ...` freezes the public target, input-file bytes,
wire settings and request/oracle hashes; `doctor --plan` verifies it, and
`run --plan` consumes only verified current snapshots. A plan hash is integrity
evidence, not authorization. Machine results and stable problem codes are
specified by `schemas/` and [agent-use.md](agent-use.md).

Profile v2 feature/effort claims declare target obligations, not implementation
progress: required, optional, unsupported or unknown, with evidence for known
claims. Missing claims are unknown; selected unknowns/provisional profiles block
bound execution. The K3.1 Pro profile is a blocked scaffold and inherits no K3
requirements. A selected KVV case is a development contract, not proof of complete
official coverage. See [framework-bringup.md](framework-bringup.md).

Every case has an independent role (`gate`, `diagnostic`, `observation`,
`platform`) and stage (`protocol`, `tooling`, `conversation`, `robustness`,
`regression`). Select with `role:`/`stage:`; multiple selectors remain a union.
The gate summary only covers selected required gates; it never hides diagnostic
failures, observations, incomplete evidence or unimplemented coverage.
Renderer token-id comparisons are a separate offline component layer. Bounded
response-binding scenarios consume prior validated call identities and fixed
canned tool results, never execute generated tool calls. Replanning selects the
whole case so dependencies stay intact; comparison is local-only.

## Learning loop

1. Reproduce and minimize an incident, with credentials removed.
2. Add one case under its primary capability.
3. Link source, regression and knowledge metadata.
4. Add a fake/negative oracle where possible.
5. Update the capability knowledge page with symptom, suspected boundary,
   confirmed cause, fix and versioned evidence.

The default smoke plan is 9 cases / 13 requests. The current endpoint catalog
has 59 cases; explicit `opt_in` acknowledgement is required for long-history
and platform-specific negative probes. Parser vectors are separate: 7 vectors
and 21 offline chunk trials, with no bundled engine adapter. See
`docs/migration.md` and `sources/provenance.yaml` for the retained/extracted
boundary.

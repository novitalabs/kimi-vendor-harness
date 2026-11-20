# kimi-agentic-harness

A capability and regression harness for deploying Kimi models on different
inference engines. It checks engine-facing contracts used by agentic systems:

- tool calls and `tool_choice` (`none` / `auto` / `required`);
- dynamic tools, JSON arguments and JSON Schema constrained output;
- `json_object` / `json_schema` response formats and tools combinations;
- thinking/reasoning fields and channel boundaries;
- streaming SSE, tool-call deltas, finish reasons and usage;
- Kimi XTML prompt/parser vectors and prompt-token consistency;
- multi-turn tool-result state, error envelopes and real regressions.

Passing these tests does **not** establish planning, reasoning, coding, vision,
long-context, or benchmark quality. BEAM, MMMU Pro, OCRBench, DeepSWE and the
former vendor acceptance/score orchestration are intentionally outside this
repository.

## Install

Every user owns an environment outside the checkout:

```bash
uv venv "$HOME/.cache/kimi-agentic-harness/venv" --python 3.12 --allow-existing
UV_PROJECT_ENVIRONMENT="$HOME/.cache/kimi-agentic-harness/venv" uv sync --locked
export NVST_HARNESS_PYTHON="$HOME/.cache/kimi-agentic-harness/venv/bin/python"
export NVST_RESULTS_ROOT="$HOME/.cache/kimi-agentic-harness/runs"
```

Set `KIMI_API_KEY` only in your shell or use `scripts/nvst_credentials.py`.
Access metadata is secret-free; see `cases/service-access.example.json`.

## Zero-traffic plan

```bash
"$NVST_HARNESS_PYTHON" -m novita_self_test capabilities plan \
  --model kimi-k3 --select smoke
"$NVST_HARNESS_PYTHON" -m novita_self_test doctor \
  --model kimi-k3 --select smoke --max-requests 32
"$NVST_HARNESS_PYTHON" -m novita_self_test list \
  --model kimi-k3 --select tool_call/choice
```

Selectors include case IDs, capability paths, `tag:<tag>`,
`component:<implementation-boundary>`, `smoke`, and glob patterns. The plan
reports request count, output-token budget, source, knowledge links and likely
code boundaries before any endpoint traffic.

## Explicit endpoint run

```bash
"$NVST_HARNESS_PYTHON" -m novita_self_test capabilities run \
  --model kimi-k3 \
  --service-access cases/service-access.json \
  --select tool_call/choice \
  --max-requests 8 \
  --execute
```

`--execute` is mandatory. There are no implicit retries or official repeat
waves. A run is bounded by `--max-requests`, writes case-level request/response
artifacts under `NVST_RESULTS_ROOT`, and redacts credentials. A run looks like:

```text
runs/kimi-k3/<run-id>/
├── plan.json
├── service-access.json       # public metadata only
├── report.json
├── report.md
└── <case-id>-<repeat>-<mode>-<step>.json
```

Each case artifact contains the redacted request, parsed response, assertion
result, provenance and knowledge links. HTTP auth/quota,
transport, timeout and 5xx failures are execution/service evidence, not model
failures. An invalid-schema case may intentionally expect a 4xx; a 5xx remains
a failed contract.

## Offline parser conformance

Parser vectors are a separate framework adapter contract. They do not test an
endpoint or prove model quality:

```bash
"$NVST_HARNESS_PYTHON" scripts/validate-parser-conformance.py
"$NVST_HARNESS_PYTHON" scripts/run-parser-conformance.py \
  --adapter 'python /path/to/framework_parser_adapter.py'
```

## Layout

```text
capabilities/         capability-owned JSONL endpoint cases
knowledge/             implementation guidance and regression history
profiles/              model capability declarations
sources/               provenance and adaptation records
src/novita_self_test/  case catalog, bounded runner and safe access handling
schemas/               access and case schemas
scripts/               credentials and parser conformance tools
tests/                 harness unit tests
```

Case taxonomy and knowledge workflow are documented in
[`docs/capability-taxonomy.md`](docs/capability-taxonomy.md). The K31 tool-call
porting guidance is decomposed into `knowledge/` pages; unverified claims remain
marked as such. See `docs/migration.md` and `sources/provenance.yaml` for scope
and source boundaries.

## Development

```bash
"$NVST_HARNESS_PYTHON" -m pytest -q tests
```

Add a minimized production regression under its primary capability, attach
source/provenance and knowledge links, add a fake/negative oracle where possible,
and record the likely engine boundary. Do not add a benchmark score as a
capability pass condition.

# kimi-agentic-harness

A capability and regression harness for deploying Kimi models on different
inference engines. It checks engine-facing contracts used by agentic systems.
The current catalog contains **58 endpoint cases**. The default `smoke`
selection contains **9 cases / 13 HTTP requests** for `kimi-k3` and
`kimi-k3-flash`; larger selections are explicit and are not benchmarks.

It checks:

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

Set `KIMI_API_KEY` only in your shell; the runner reads the environment variable
named by the access file. The optional registry helper does not auto-populate
that variable; see [`docs/credentials.md`](docs/credentials.md). Access metadata
is secret-free; see `cases/service-access.example.json`.

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
code boundaries before any endpoint traffic. `list` defaults to all catalog
cases; `plan`, `doctor`, and `run` default to `smoke`. Directory selectors include
all subdirectories, including negative tests, so inspect the plan first.

Profiles have one applicability contract: flat `features` and `efforts` in
`profiles/*.yaml`. All four commands use it. Missing requirements are shown in
`list`/`plan`, block `doctor`, and yield `unsupported` without sending those
requests in `run`. `doctor --service-access <file>` additionally validates the
access file, matching model family, and credential environment variable; it does
not contact the endpoint. `served_model` may be a deployment-specific alias.

## Explicit endpoint run

```bash
"$NVST_HARNESS_PYTHON" -m novita_self_test capabilities run \
  --model kimi-k3 \
  --service-access cases/service-access.json \
  --select tool_call.choice.none \
  --select tool_call.choice.required \
  --max-requests 4 \
  --execute
```

`--execute` is mandatory. There are no implicit retries or official repeat
waves. A run is bounded by `--max-requests`, writes case-level request/response
artifacts under `NVST_RESULTS_ROOT`, and redacts credentials. Cases tagged
`opt_in` (long-history and platform-specific negative probes) additionally
require `--allow-opt-in`; this is a traffic acknowledgement, not a claim that
the target supports the feature. Concurrency is 1; `--timeout` is the total
wall-clock limit per HTTP request, and responses are capped at 16 MiB. Auth,
quota, rate-limit, transport, and server failures stop further requests.
A run looks like:

```text
runs/kimi-k3/<run-id>/
├── plan.json
├── service-access.json       # public metadata only
├── report.json
├── report.md
└── <case-id>-<repeat>-<group>-<mode>-<step>.json
```

Each case artifact contains the redacted request, parsed response, assertion
result, provenance and knowledge links. HTTP auth/quota,
transport, timeout and 5xx failures are execution/service evidence, not model
failures. An invalid-schema case may intentionally expect a 4xx; a 5xx remains
a failed contract. `observed` is reserved for deliberately non-deterministic
or black-box observation cases; it is not a green capability pass. Output
truncation normally yields an `inconclusive` case / `incomplete` report. Transport
interruptions yield `execution_invalid` and retain received bytes. The dedicated
output-cap case allows intentional truncation and checks reported usage only.

Run exit codes: **0** = passed; **1** = contract/assertion failure; **2** =
configuration, service/execution invalid, unsupported/incomplete, or observation
only. Inspect `report.json` for per-case status and the stop reason.

## Offline parser conformance

Parser vectors are a separate framework adapter contract. They do not test an
endpoint or prove model quality:

```bash
"$NVST_HARNESS_PYTHON" scripts/validate-parser-conformance.py
"$NVST_HARNESS_PYTHON" scripts/run-parser-conformance.py \
  --adapter 'python /path/to/framework_parser_adapter.py' \
  --adapter-timeout 30
```

The adapter consumes JSONL from stdin, emits one result per request in order,
and must exit with code 0 after EOF. `--adapter-timeout` is a total process
wall-clock limit (30 seconds by default). Hung adapters and their child process
group are killed; stderr cannot deadlock the exchange. No framework parser
adapter is bundled, so a successful synthetic adapter self-test is not evidence
that an engine parser conforms.

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
and source boundaries. The parser vector file contains **7 vectors / 21
chunk-size trials**; vector validation is a file-contract check, while adapter
execution is a separate offline framework check.

## Coverage boundaries

- The selected `$ref`, nested schema, dynamic-tool, and response-format cases
  are adapted feature probes, not the complete KVV/Walle corpus or official
  acceptance. Platform-specific negative probes are explicitly tagged.
- Literal-marker usage and conflicting reasoning fields are observations, not
  proof of special-token identity or precedence. Exact prompt-token and
  tool-result ordering checks need a pinned checkpoint encoder/tokenizer and a
  token-aware adapter, which this repository does not provide.
- Multimodal transport/token fixtures and a K3.1 profile are not implemented.
  Knowledge pages include engineering guidance and gaps, not proof of coverage.
- Invalid-input cases expect graceful client errors; a buggy engine could still
  crash. Run only against a target where the selected negative probes are safe.

## Development

```bash
"$NVST_HARNESS_PYTHON" -m pytest -q -p no:cacheprovider tests
```

Add a minimized production regression under its primary capability, attach
source/provenance and knowledge links, add a fake/negative oracle where possible,
and record the likely engine boundary. Do not add a benchmark score as a
capability pass condition.

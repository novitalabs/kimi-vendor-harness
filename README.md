# kimi-agentic-harness

A capability and regression harness for deploying Kimi models on different
inference engines. It checks engine-facing contracts used by agentic systems.
The current catalog contains **59 endpoint cases**. The default `smoke`
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
export PATH="$HOME/.cache/kimi-agentic-harness/venv/bin:$PATH"
export NVST_HARNESS_PYTHON="$HOME/.cache/kimi-agentic-harness/venv/bin/python"
export NVST_RESULTS_ROOT="$HOME/.cache/kimi-agentic-harness/runs"
```

Set `KIMI_API_KEY` only in your shell; the runner reads the environment variable
named by the access file. The optional registry helper does not auto-populate
that variable; see [`docs/credentials.md`](docs/credentials.md). Access metadata
is secret-free; see `cases/service-access.example.json`.

## Test entry points

`kimi-harness` is the only installed test command:

| Command | Purpose | Endpoint traffic |
|---|---|---|
| `kimi-harness plan` | Select endpoint cases, inspect applicability and budgets | None |
| `kimi-harness doctor` | Validate local configuration, access metadata and credential env | None |
| `kimi-harness run` | Execute endpoint cases with bounded HTTP requests | Requires `--execute` |
| `kimi-harness parser validate` | Validate the parser-vector file only | None |
| `kimi-harness parser run` | Execute vectors through a framework-supplied local adapter | None |
| `kimi-harness renderer validate/run` | Validate pinned goldens / compare renderer token ids | None |
| `kimi-harness compare` | Compare existing baseline/candidate reports | None |

The `tests/` pytest suite is for **harness development**, not an alternative way
to execute all endpoint cases. Its HTTP integration checks use loopback fakes.
For code agents, start with [docs/agent-use.md](docs/agent-use.md): machine-output
schemas, frozen-plan checks, authorization/stop rules and untrusted-data safety.

## Zero-traffic plan

```bash
kimi-harness plan --model kimi-k3 --select smoke
kimi-harness doctor --model kimi-k3 --select smoke --max-requests 13
kimi-harness plan --model kimi-k3 --select tool_call/choice --format summary
# Inspect the complete catalog without executing it:
kimi-harness plan --model kimi-k3 --select all --format summary
```

Selectors include case IDs, capability paths, `tag:<tag>`,
`component:<implementation-boundary>`, `role:<role>`, `stage:<stage>`, `smoke`, and glob patterns.
For example, `--select role:gate` selects gate cases and
`--select stage:conversation` selects conversation scenarios. Multiple selectors
are a union, not an intersection. The plan
reports request count, output-token budget, source, knowledge links and likely
code boundaries before any endpoint traffic. `plan`, `doctor`, and `run`
default to `smoke`; the complete catalog must be explicitly selected with
`--select all`. `plan --format summary` provides a human-readable case list;
the default `--format json` retains complete source/knowledge and case metadata.
`--out <file>` saves the chosen format. Directory selectors include all
subdirectories, including negative tests, so inspect the plan first.

Profile v2 `features`/`efforts` are **target expectations**, not implementation
progress: `required`, `optional`, `unsupported`, or `unknown`, with evidence
references for known claims. Missing claims are unknown, not silently unsupported.
Deleting an unimplemented requirement therefore blocks execution instead of
making the framework look compliant. Profiles also record authority references
and unknown checkpoint/encoder/tokenizer information.

`kimi-k3` and `kimi-k3-flash` retain their pinned API feature scope. The
`kimi-k31-pro` profile is an explicitly **provisional, blocked scaffold**: no
K3/Flash token ids, effort support or API policy is inherited. Obtain checkpoint
and API evidence and review the profile before enabling it. See
[framework-bringup.md](docs/framework-bringup.md).

All endpoint commands use the same applicability contract. Known unsupported
cases are skipped and keep the overall report incomplete; selected unknown
requirements block target-bound execution with zero traffic. `doctor` also
checks access metadata, model family and credential env; it does not contact
the endpoint. `served_model` may be a deployment-specific alias.

## Reviewed frozen-plan execution

Planning is zero traffic and does not need a credential value. Add public access
metadata to bind a target and make the plan executable:

```bash
kimi-harness plan \
  --model kimi-k3 \
  --select tool_call.choice.required \
  --service-access cases/service-access.json \
  --max-requests 2 --timeout 60 \
  --out "$NVST_RESULTS_ROOT/required-plan.json"
kimi-harness doctor \
  --plan "$NVST_RESULTS_ROOT/required-plan.json" \
  --service-access cases/service-access.json
# Only after the caller approves this target/scope/aggregate request allowance:
kimi-harness run \
  --plan "$NVST_RESULTS_ROOT/required-plan.json" \
  --service-access cases/service-access.json \
  --execute
```

Plans without `--service-access` are discovery-only. Frozen plans bind harness
source, schemas, case/profile/fixture bytes, exact request-template/oracle hashes,
approved response-binding rules, public target identity and execution settings.
For response-bound multi-turn scenarios the future call ids are unknown at
planning time; evidence records the actual materialized request hash separately. Detected drift/integrity mismatch rejects
before traffic; hashes are not signatures or a protection against a caller
creating/replacing a valid plan.
`run --plan` forbids model/select/repeat/timeout overrides; `--max-requests` may
only lower the frozen ceiling. A hash/plan/flag is not permission to execute;
the caller must authorize and account for requests across runs. See the agent
protocol for target-attestation limits and credential rotation.

## Direct endpoint run

For interactive use, direct selection creates a target-bound plan immediately
before execution, with the same request ceilings and safety gates:

```bash
kimi-harness run \
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

Default stdout is one JSON document for every command, including argument and
configuration errors. `--help` and `plan --format summary` are human views;
stderr is diagnostic, not the result. Schemas are in `schemas/`: plan v3,
profile v2, endpoint report/evidence and parser report v2, doctor/error/renderer/
comparison v1. Old frozen plans/reports are not silently migrated. Problems have stable `code`, `message`,
`expected`, `actual`, and `path` fields; agents must not parse message text.

Run exit codes: **0** = passed/plan ready; **1** = contract/assertion failure;
**2** = blocked, service/execution invalid, unsupported/incomplete, or observation
only. Inspect JSON `kind`/`status`, per-case codes, and `stop_reason`. Reports
include `plan_sha256`; evidence hashes refer to pre-redaction requests.

Cases declare separate roles: `gate` (format/contract checks), `diagnostic`
(model-dependent behavior), `observation` (no pass claim), and `platform`
(explicit platform policy). `report.gate_summary` reports **selected required
gates only**, independently of observations/diagnostics. It is not complete
model readiness, and cannot override invalid execution or missing coverage.
The overall status still preserves all selected failures/incomplete results.

HTTP evidence includes start/end/first-byte timing, received-byte count and
response-byte hash. Invalid UTF-8 retains the offending byte range/hex and cannot
pass. Known protocol errors remain failed even when generation is truncated.

## Offline parser conformance

Parser vectors are a separate framework adapter contract. They do not test an
endpoint or prove model quality:

```bash
kimi-harness parser validate
kimi-harness parser run \
  --adapter 'python /path/to/framework_parser_adapter.py' \
  --timeout 30
```

The adapter consumes JSONL from stdin, emits one result per request in order,
and must exit with code 0 after EOF. `--timeout` is a total process
wall-clock limit (30 seconds by default). Hung adapters and their child process
group are killed; stderr cannot deadlock the exchange. No framework parser
adapter is bundled, so a successful synthetic adapter self-test is not evidence
that an engine parser conforms. Each committed vector explicitly selects
`comparison: json` (typed JSON-value equivalence) or `bytes` (raw JSON argument
preservation). Text/channels/call order still compare exactly.

## Framework bring-up and repair loop

See [docs/framework-bringup.md](docs/framework-bringup.md) for contract evidence,
renderer goldens/adapter protocol and bounded response-binding scenarios.

```bash
# Offline: developer supplies checkpoint-generated, hash-pinned goldens.
kimi-harness renderer validate --vectors /path/to/goldens.jsonl
kimi-harness renderer run --vectors /path/to/goldens.jsonl \
  --adapter 'python /approved/framework_adapter.py' --timeout 30

# Offline: select whole failed scenarios for a NEW reviewed plan/target.
kimi-harness plan --model kimi-k3 --failed-from /path/to/baseline/report.json \
  --service-access cases/service-access.json --max-requests 4 \
  --out "$NVST_RESULTS_ROOT/retest-plan.json"

# After separately authorized execution, compare the reports without traffic.
kimi-harness compare --baseline /path/to/baseline/report.json \
  --candidate /path/to/candidate/report.json
```

Replanning includes every step/mode of each selected case, so dependencies are
not dropped. It never runs tests, reuses a stale plan or retries automatically.
Service/execution-invalid reports are not a source for automatic failed-case
selection. Comparison marks changed specs/scope as not comparable; candidate
source/runtime changes are allowed only through a new plan. Its `passed` means
comparison completed without a regression/spec mismatch, **not** candidate
acceptance; unchanged failures remain failures.

## Layout

```text
capabilities/         capability-owned JSONL endpoint cases
knowledge/             implementation guidance and regression history
profiles/              model capability declarations
sources/               provenance and adaptation records
src/novita_self_test/  case catalog, bounded runner and safe access handling
schemas/               access and case schemas
scripts/               optional private credential utility
tests/                 harness unit tests and loopback HTTP checks
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
  tool-result ordering checks need checkpoint-generated goldens and an approved
  framework renderer adapter. The protocol/bridge is supplied; actual K3.1 Pro
  checkpoint artifacts and goldens are not bundled.
- Dedicated multimodal endpoint fixtures are not implemented. K3.1 Pro has only
  an unknown/blocked profile scaffold, not verified support.
  Knowledge pages include engineering guidance and gaps, not proof of coverage.
- Invalid-input cases expect graceful client errors; a buggy engine could still
  crash. Run only against a target where the selected negative probes are safe.

## Development

A two-request local exercise of the complete agent workflow (no endpoint URL
input, synthetic key, deliberately undeclared tool, no repair/retry) is available:

```bash
"$NVST_HARNESS_PYTHON" examples/agent-loopback.py --out /path/to/new/task-output
```

The example succeeds only when the expected contract failure is found and the
report/evidence is linked. This is not a real-engine pass.

Run the harness self-tests (unit, synthetic adapter, and loopback HTTP), not
live endpoint acceptance:

```bash
"$NVST_HARNESS_PYTHON" -m pytest -q -p no:cacheprovider tests
```

Add a minimized production regression under its primary capability, attach
source/provenance and knowledge links, add a fake/negative oracle where possible,
and record the likely engine boundary. Do not add a benchmark score as a
capability pass condition.

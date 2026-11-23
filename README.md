# kimi-agentic-harness

A Kimi engine-facing capability and regression harness: tool calls, structured
output, reasoning channels, SSE, usage, prompt/parser behavior and multi-turn
state. It is **not** vendor acceptance or a planning, reasoning, coding, vision
or long-context quality benchmark.

The catalog has **59 endpoint cases**. Default `smoke` is **9 cases / 13 HTTP
requests** for `kimi-k3` and `kimi-k3-flash`. `kimi-k31-pro` is a **provisional,
blocked contract scaffold**, not verified support; it inherits no K3/Flash token
ids, efforts or API policy.

## Install

Use a user-owned environment outside the checkout. Run from the repository:

```bash
UV_PROJECT_ENVIRONMENT="$HOME/.cache/kimi-agentic-harness/venv" uv sync --locked --python 3.12
export PATH="$HOME/.cache/kimi-agentic-harness/venv/bin:$PATH"
export NVST_HARNESS_PYTHON="$HOME/.cache/kimi-agentic-harness/venv/bin/python"
export NVST_RESULTS_ROOT="$HOME/.cache/kimi-agentic-harness/runs"
```

Repeat `uv sync --locked` with the same external environment to update. No KVV
checkout, benchmark pack or shared build host is required. Framework component
adapters/checkpoint artifacts are supplied separately by the developer.

Copy [`cases/service-access.example.json`](cases/service-access.example.json)
to an ignored access file and set its `api_key_env` variable in your shell. The
runner does not auto-load `.env` or a private registry. Never put credentials in
access metadata or argv; see [credentials](docs/credentials.md).

## CLI

`kimi-harness` is the only installed test command; use `--help` for options.

| Command | Purpose | Endpoint traffic |
|---|---|---|
| `plan` | Select cases, inspect requirements and budgets, freeze a target | None |
| `lint` | Validate all cases or exact files/ids with source locations | None |
| `doctor` | Verify local configuration, credential env and saved plan | None |
| `run` | Execute bounded endpoint cases | Requires `--execute` |
| `parser validate/run` | Validate vectors / run an approved local parser adapter | None |
| `renderer validate/run` | Validate goldens / compare renderer token ids | None |
| `compare` | Compare existing baseline/candidate reports | None |

### Inspect, freeze, authorize, run

```bash
kimi-harness plan --model kimi-k3 --select all --format summary
kimi-harness plan --model kimi-k3 \
  --select tool_call.choice.required \
  --service-access "$ACCESS" --max-requests 2 --timeout 60 \
  --out "$PLAN"
kimi-harness doctor --plan "$PLAN" --service-access "$ACCESS"
# Only after the caller approves target, scope, time window and aggregate budget:
kimi-harness run --plan "$PLAN" --service-access "$ACCESS" --execute > "$REPORT"
```

`$ACCESS`, `$PLAN` and `$REPORT` are caller-chosen paths. Use JSON plans for
execution; unbound plans are discovery-only. `run --plan` forbids model/select/
repeat/timeout overrides; its request ceiling may only be lowered. Changed code,
fixtures, profiles, wire configuration or public target identity need a new plan.
Plan hashes detect drift, not authorization or live-backend identity.

Selectors accept ids, capability paths, globs, `tag:`, `component:`, `role:` and
`stage:`. Multiple `--select` values form a **union**; plan's `--filter` values
are AND refinements, and `--exclude` subtracts matches. Unknown selectors return
suggestions, never auto-select. Endpoint commands default to `smoke`; `all` is
explicit. Inspect directory selections because they include negative tests.
Roles distinguish gates, diagnostics, observations and platform policy;
`gate_summary` covers selected required gates only.

```bash
# Focus on gate cases involving the tool parser; no endpoint traffic.
kimi-harness plan --select role:gate --filter component:tool_parser --format catalog
# Edit a case at the file:line shown by catalog/summary, then validate locally.
kimi-harness lint --file capabilities/tool_call/choice/cases.jsonl --id tool_call.choice.required
kimi-harness lint  # full catalog before sharing changes
```

`--format catalog` is a compact JSON discovery view with locators, requirements,
source and knowledge links, not a saved execution plan. Only default JSON plans
can execute. Start new cases from [the template](examples/case-template.json);
see [case authoring](docs/capability-taxonomy.md).

Safety limits remain: concurrency 1, no implicit retry, request/output ceilings,
per-request wall-clock timeout, 16 MiB response cap, and stop on service/transport
failures. Long-history/platform cases tagged `opt_in` also need
`--allow-opt-in`. Negative probes require a target where they are safe.
Credentials, doctor success and `--execute` are not permission to send traffic.

Each run writes public plan/access metadata, `report.json`, `report.md`, and
redacted per-request evidence under `NVST_RESULTS_ROOT` (default ignored `runs/`).
Default stdout is one versioned JSON document, including errors; `--help` and
`plan --format summary` are human views. Exit 0 means ready/passed, 1 failed,
2 blocked/invalid/incomplete/observed. Interpret `kind`, `status` and stable
problem codes, not prose. Full schemas, stop rules and evidence semantics are in
the [agent protocol](docs/agent-use.md).

## Offline components and repair

```bash
kimi-harness parser validate
kimi-harness parser run --adapter 'python /approved/parser_adapter.py' --timeout 30
kimi-harness renderer run --vectors /pinned/goldens.jsonl \
  --adapter 'python /approved/renderer_adapter.py' --timeout 30
kimi-harness plan --model kimi-k3 --failed-from /baseline/report.json --repeat 1 \
  --service-access "$NEW_ACCESS" --max-requests 4 --out "$RETEST_PLAN"
kimi-harness compare --baseline /baseline/report.json --candidate /candidate/report.json \
  --scope candidate
```

Adapters run approved local code, not sandboxed code. The seven parser vectors /
21 chunk trials are reference-derived; no certified engine adapter or K3.1 Pro
checkpoint golden is bundled. File validation is not component conformance.
Replanning selects whole scenarios but never executes/retries them. Retests
require an explicit `--repeat`; the new plan records the baseline and any repeat
change for review. Compare checks each mode/step/repeat, so a fix cannot conceal
a new regression. Default scope is all cases; explicit `--scope candidate`
compares retested cases and lists the rest as `not_retested`, not passed.
Changed specifications or unit coverage remain not comparable. No comparison
status is candidate acceptance.

## Documentation and development

- [Framework bring-up](docs/framework-bringup.md): authority, target obligations,
  component goldens, bounded multi-turn scenarios and repair/retest.
- [Agent protocol](docs/agent-use.md): authorization, frozen plans, JSON and stops.
- [Taxonomy](docs/capability-taxonomy.md): case organization and authoring.
- [Knowledge](knowledge/README.md): implementation boundaries and evidence hierarchy.
- [Source scope](docs/migration.md) and [provenance](sources/provenance.yaml):
  retained/extracted material, not complete KVV/Walle/official coverage.

Harness self-tests use synthetic adapters and loopback HTTP, **not real-engine
acceptance**. A separate two-request example intentionally detects a faulty tool:

```bash
"$NVST_HARNESS_PYTHON" -m pytest -q -p no:cacheprovider tests
"$NVST_HARNESS_PYTHON" examples/agent-loopback.py --out /path/to/new/demo-output
```

For regressions, add a minimized case, source/knowledge links and a negative
oracle test. Never weaken expectations just to make an implementation green.

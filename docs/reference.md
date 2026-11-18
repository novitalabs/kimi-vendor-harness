# Operator Reference

Detailed contracts behind `nvst`. Start with the top-level `README.md`.

`novita-self-test` is the single multi-model entrypoint for Kimi vendor
development diagnostics and formal acceptance. It executes official KVV from
`vendor/` and owns Novita API tests, Code Bench, deepSWE integration,
profiles, orchestration, artifact contracts, and reporting.

## Quick start

```bash
uv sync
uv run nvst list --model kimi-k3
uv run nvst doctor --model kimi-k3 \
  --service-access cases/service-access.json \
  --judge-access cases/judge-access.json
# Explicit bounded endpoint/judge traffic and host/Docker canaries.
uv run nvst doctor --model kimi-k3 \
  --service-access cases/service-access.json \
  --judge-access cases/judge-access.json --online-canary
uv run nvst develop --model kimi-k3 \
  --service-access cases/service-access.json --select 1.0
uv run nvst develop --model kimi-k3 \
  --service-access cases/service-access.json \
  --select 7 --select 8 --select 9
uv run nvst accept --model kimi-k3 \
  --service-access cases/service-access.json \
  --judge-access cases/judge-access.json --acceptance simple
uv run nvst accept --model kimi-k3 \
  --service-access cases/service-access.json \
  --judge-access cases/judge-access.json --acceptance full
uv run nvst accept --model kimi-k3 \
  --service-access cases/service-access.json \
  --judge-access cases/judge-access.json --acceptance k3-sop
# Development-only scenario override
uv run nvst develop --model kimi-k3 --service-access cases/service-access.json \
  --select 17 --concurrency 50
```

`kimi-k3` is pinned to
the ordered `K3-vendor-test-result` table at Docx revision 444, the Stream spec
self-validation file at Docx revision 244, and toolcall thinking-effort rows
from Serving Requirements Docx revision 1200 / embedded Sheet revision 170.
`kimi-k3-flash` has a separate profile pinned to Moonshot's K3-flash vendor
contract at document revision 122, interface/accuracy sheets revision 34, and
shared Stream spec revision 244. Its KVV-derived suites execute the official
shared `MoonshotAI/kvv-k3` `k3` checkout at commit
`6ec0eaad1ca98933327cf571c0d8b78ab8476551`, including its Git LFS fixtures.
The same K3-family admission library is shared by Kimi K3, Kimi K3 Flash, and
Kimi K3.1; it is vendored at `vendor/kvv-k3`. K3 and Flash
KVV-derived suites execute from that snapshot. The shared source inventory is 19 Params, 428
Walle, 118 K3-feature, 74 prompt-token, and 71 stable2 toolcall samples (355
trials across five epochs). Flash runs rows 1.0/1.1 and 2..10: the three
benchmark families are max-only, BEAM is excluded, and Kimi Code Bench/Moonshot
stress tests remain external closed acceptance gates.

K3 Stream p0/p1/p2 use the exact self-validation file embedded in Moonshot's
`Streaming Output Spec List for Vendor` Docx revision 244. The file is the
acceptance boundary: prose items it does not test default to passed. BEAM uses
the secret-safe judge-access contract with profile-pinned endpoint, model, and
RPM; its credential is named by `PPIO_JUDGE_API_KEY`. Successful terminal empty
vendor responses score zero, while endpoint and judge request errors remain
execution-invalid. deepSWE verifies the local source commit, clean tracked worktree,
Pier version, and 113-task inventory before staging or execution. It runs only
on a host with the prepared DeepSWE runtime (`transport: local`). The legacy K2.x replay suite is not part of the K3 test set.
K3 full acceptance runs required precision rows 7..15 and 17; extended Kimi
Code Bench and BEAM are optional explicit suites and are not automatically run.

`--acceptance k3-sop` is the pre-launch gate defined by Moonshot's K3 上线 SOP
(Docx revision 9). It runs the required checks in the SOP order: tool-call
thinking effort, OCRBench, MMMU Pro, K3 features, prompt tokens, Params,
tool-call JSON schema, Streaming P0/P1, and the current Novita-owned required
coverage (Image, DeepSWE, Row 18). BEAM and extended Kimi Code Bench are optional
explicit add-ons. It deliberately excludes Streaming P2 because the SOP names
P0 and P1. All selected suites remain fail-fast and a passed run is the test
gate before the separate 10% -> 30% -> 100% rollout process.

K3 suites are classified into required feature tests (Rows 1.0-6 and 18),
required precision tests (Rows 7-15 and 17), and optional precision suites
(Row 16 BEAM and K3-CB). Feature tests are bounded contract checks intended
for fast feedback; precision tests are benchmark-style runs with larger sample
sets, score thresholds, and longer execution time. See
[`docs/k3-test-catalog.md`](docs/k3-test-catalog.md) for the row-by-row contract.

`K3` is pinned to active rows `1.0`, `1.1`, `1.2`, then `2..18`. The three
row 1 subrows run Stream spec P0/P1/P2. Row 18 is the Novita-owned toolcall
arguments recovery contract; it runs after the feature group in the same full
acceptance and is no longer supplementary. Case A checks non-empty
valid JSON arguments after replaying 143 of 157 messages, rejects the sample
forbidden pattern, and does not enforce a tool name. Case B accepts non-empty
valid JSON `WriteFile` or `SetTodoList` arguments after replaying 19 of 21
messages. Streaming aggregates all deltas by `tool_calls[].index` and judges
index 0 against the non-stream branch.

BEAM generation is the vendor-facing phase. Once its 700 answers are complete,
the judge runs as a background process without sending more
requests to the tested service, so the next acceptance suite can start. The
final run report waits for that judge and records both phases.

The row-by-row production contract is documented in
[`docs/k3-row-contracts.md`](docs/k3-row-contracts.md). It is the review index
for each entrypoint, request/worker concurrency, official versus profile-owned
retry policy, and required result artifacts.

`src/novita_self_test/stability.py`. Quota/auth failures stop traffic as
`service_invalid`; 429, overload, timeout, connection, and 5xx outcomes use the
owning runner's retries and can never be converted into a model score or explicit
behavior failure. Any suite marked `failed` stops the current simple, full, or
develop plan immediately; later suites are recorded as blocked/not-run evidence.

K3 exposes three profile-owned scopes. Precision tests run before feature tests.
The official precision group is Rows 7..17; the extended Kimi Code Bench suite
is supplementary precision evidence. The feature group is Rows 1.0/1.1/1.2
through 6 plus Row 18. `--acceptance simple`
runs OCRBench rows 10..12, BEAM row 16, one DeepSWE round, then the feature
group. `--acceptance full` runs all precision rows and extended Kimi Code Bench,
then the complete feature group; DeepSWE uses three rounds. `develop` keeps
explicit `--select` selectors and uses one DeepSWE round when row 17 is selected.

Every suite directory contains `suite-input.json` alongside
`suite-result.json`. DeepSWE additionally writes `task-results.jsonl`, Pier
result paths, and per-trial request evidence with secret-safe request/response
bodies, hashes, status/headers, request IDs, and available replica/rank/route
identity.

Each run also produces a delivery directory at `test-artifacts/`. Its
`bundle.json` is the top-level inventory and completeness summary. Every
numbered suite directory under it contains the non-secret suite inputs, the
normalized result, a `suite-manifest.json`, and copied task/case/evidence files;
evidence outside the run directory is placed under `artifacts/external/`.
The suite manifest also indexes the effective parameters and the expected
versus observed versions/commits of external dependencies (for example Harbor,
Terminal Bench 2, Kimi CLI, Pier, DeepSWE, and the Code Bench runner), with
unresolved observed values left explicit rather than invented from pins.
Missing files and intentionally skipped secret-bearing files remain visible in
the manifests, while credential material is never copied. Evidence copying is
allowlisted to readable structured formats (`.json`, `.jsonl`, `.log`, `.md`,
`.txt`, `.csv`, `.tsv`, `.yaml`, `.sh`): bulk machine formats such as DeepSWE's
`.eval` payloads stay on the execution host and are recorded by path, size, and
`skipped: non_deliverable_artifact` instead of being shipped. `bundle.json` also
states the observed `served_model` and the run `started_at`, because one profile
family can front several deployments. This directory can be handed to an
acceptance reviewer without requiring the live run tree.

`nvst collect` consolidates committed runs along that model × time axis:

```bash
nvst collect --model kimi-k3                          # index.json + index.md
nvst collect --model kimi-k3 --served-model moonshotai/kimi-k3-h
nvst collect --model kimi-k3 --copy-bundles --out package/  # stage the bundles
```

It groups runs by the observed served model, orders them by start time, and
flags a run that served more than one deployment rather than merging it. With
`--copy-bundles` each run's `test-artifacts/` directory is staged under
`<served-model>/<started-at>-<run-id>/`, so the package follows the same axis as
the index.

Before a formal run, `nvst doctor` performs a zero-vendor-traffic validation of
execution site, profile inputs, credential environment, repository cleanliness,
runner entrypoints, BEAM judge contract/RPM, and DeepSWE runtime pins.
`scripts/nvst_run_control.py` supports durable `start`, `status`, `pause`,
`resume`, and `stop`. Managed runs use a transient user-systemd service with cgroup-scoped
pause/stop and a one-shot 0600 FIFO for endpoint and judge credentials. An
explicit `NVST_ALLOW_PROCESS_FALLBACK=1` is required to use process-group
fallback. Mutable artifacts use file locks, monotonic revisions, writer
identity, and schema validation. `run.json` is the current snapshot;
`events.jsonl` is the secret-scanned append-only lifecycle record.

K3 rows 3-6 each define 10 independent pytest rounds. Each repeat uses its
case-count concurrency and a barrier before the next repeat. Official rerun policy remains authoritative:
Walle keeps `--reruns 3`, K3 Features keeps its per-case markers, while Params
and Prompt Tokens use profile-owned formal plus at most three batch any-pass
waves because they have no official case rerun policy. Every round must still
meet its original expected counts and artifacts.

The profile's concurrency values are the defaults: Row 2 stays serial, Rows 3..6
use case-count concurrency per repeat, and Rows 7..15 use up
to 900 concurrent requests. Only `nvst develop` accepts an optional
`--concurrency N`; `nvst accept --acceptance simple|full` rejects that option and
uses profile values. The develop override is applied to supported runner flags
(`-n`, `--max-connections`, and `--concurrency`) and recorded in `run.json`.
Stream/Image suites without a runner concurrency flag keep their normal behavior.
deepSWE always uses its committed manifest concurrency 113; the generic
`--concurrency` override is ignored for DeepSWE. Each DeepSWE job runs one task,
and same-task rounds are serialized with a task-local lock. A round barrier
waits for every task and its error-only retries before the next round starts.
Each task gets one formal attempt plus at most one error-only retry wave, with
retry waves beginning after the round's formal work completes. Docker keeps
`bip=172.17.0.1/16` for its proxy, allocates `/28` networks from
`192.168.0.0/16`, and passed a 410-network capacity probe.

K3 rows 7-9 run the same pinned 113-sample toolcall format benchmark with
thinking effort low, high, and max. Rows 10-12 are OCRBench, rows 13-15 are MMMU Pro, row 16 is BEAM, row 17 is
DeepSWE, and row 18 is official toolcall-arguments recovery. BEAM terminal
completions
with empty public content are valid model outputs scored as zero; request errors
remain execution-invalid. Judge traffic is globally rate-limited by the profile,
records only numeric scores as completed checkpoints, and resumes without
regenerating vendor answers. DeepSWE checkpoints every logical
round/task/attempt; resume reuses reward 0/1 results and reruns only missing
trials. The runner is a Novita-owned
behavioral reconstruction from the supplied `.eval` artifacts; its scorer was replayed
sample-by-sample against both artifacts, but prior logs never satisfy a current
run.

When an explicitly approved contract migration occurs mid-acceptance, an
acceptance session can merge only hash-compatible suite evidence:

```bash
nvst session create --model kimi-k3 --service-access service-access.json \
  --judge-access judge-access.json \
  --run runs/kimi-k3/<run-a> --run runs/kimi-k3/<run-b> \
  --approval contract-approval.json
nvst session continue --session runs/kimi-k3/sessions/<session-id> \
  --run runs/kimi-k3/<replacement-run>
nvst session report --session runs/kimi-k3/sessions/<session-id>
```

Reuse requires the same service-access hash and requirements revision plus the
same per-suite input hash. A BEAM attempt additionally requires the same
judge-access hash. A profile change requires an approval naming exact
from/to contract hashes and all affected suites. Changed suites need a new
attempt. `acceptance-session.json` retains every candidate attempt and marks the
single selected attempt for each suite. Final verdicts distinguish `passed`,
`failed` (complete valid model evidence below contract), `inconclusive`
(incomplete or execution-invalid evidence), and `service_invalid`.

## Layout

```text
src/novita_self_test/       CLI, orchestration, stability classification and adapters
models/                      profile + source/rows snapshot + suite manifest
suites/novita-api/           imported Novita endpoint tests and fixtures
suites/kimi-code-bench/      vendored Code Bench suite
suites/deepswe/              Kimi Code Pier adapter and wrapper
schemas/                     service, run, and suite-result JSON schemas
runs/                        ignored run evidence
```

See `docs/contracts.md` for status, validity, completeness, report, and refresh
contracts. Repository rules are in `AGENTS.md`.

# Contracts

## Service access

`service-access.json` identifies a deployed endpoint without storing a secret.
It includes model family, base URL, served model, API-key environment-variable
name, non-secret headers, think mode, deployment/runtime IDs, and optional
source targets for diagnosis. Authorization headers, cookies, bearer tokens,
signed URLs, and literal credentials are rejected. The controller's only
persistent secret source is the locked mode-0600 registry documented in
`docs/credentials.md`.
`judge-access.json`
independently identifies the BEAM judge endpoint, model, credential environment,
and global RPM. Endpoint and judge Authorization values are separate secrets;
neither may be substituted for the other.

Endpoint Authorization is supplied only through the environment variable named
by `api_key_env` (and, for BEAM, a distinct judge variable); access metadata
never contains credentials. A run records both access hashes and rejects drift
during resume.

Before suite traffic, nvst probes both the endpoint's OpenAI-compatible
`/models` path and one minimal real `/chat/completions` request with the selected
model. `/models` alone cannot prove that authentication, quota, routing, and
model execution are usable. HTTP 401/auth failures, quota or balance exhaustion,
connection/DNS/TLS failures, repeated HTTP 429, and HTTP 5xx are
`service_invalid`; planned suite traffic is blocked. Unsupported `/models`
catalog aliases remain inconclusive when the real chat probe succeeds.

`nvst doctor` is the default zero-vendor-traffic gate before formal acceptance.
It validates the execution site, profile hashes, credential environment,
repository cleanliness, runner entrypoints, declared BEAM judge endpoint/model
and RPM, and DeepSWE runtime pins. `--online-canary` is an explicit bounded
mode that adds minimal endpoint and judge calls, one temporary Docker `/28`
network, and host disk/inode/image/fd checks; the default remains zero traffic.
Managed remote runs use `start`, `status`, `pause`, `resume`, and `stop` through
a transient user-systemd service. Credentials enter through a one-shot 0600 FIFO
and are never stored or placed in unit arguments/environment. Process-group
fallback requires explicit `NVST_ALLOW_PROCESS_FALLBACK=1`. `run.json` records
running/paused lifecycle, heartbeat, process identity, progress, and checkpoint
paths; schema-validated `events.jsonl` is the append-only lifecycle record.

`develop` resolves selectors in user order and stops after the first valid
failure, blocked result, or invalid service/runner result. It writes
`diagnosis.json`, `diagnosis.md`, and executable `reproduce.sh`.

`accept --acceptance simple` or `accept --acceptance full` resolves the
profile-owned ordered selector list. A valid model failure, reward-zero result,
low benchmark score, or suite-local execution-invalid result stops the current
acceptance immediately; remaining suites are materialized as blocked evidence.
The report still includes every planned row. Both production modes enable the
DeepSWE long test automatically; `develop` keeps explicit selectors and stops
on the first failed, blocked, or invalid result.

For K3, simple runs required OCRBench rows 10..12, one DeepSWE round, then
feature rows 1.0/1.1/1.2 through 6 and Row 18. Full runs required precision
rows 7..15 and 17, then feature rows 1.0/1.1/1.2 through 6 and Row 18 with
three DeepSWE rounds. BEAM Row 16 and extended Kimi Code Bench are optional
explicit suites and are not implicitly included in either mode.

BEAM is two-phase: generation completes all vendor requests first, then its
judge process runs in the background while the next suite executes. Acceptance
finalization waits for the judge and records generation and judge artifacts.
Each generation task may be retried up to three times when it returns an
execution-invalid response such as `finish_reason=repetition`; the runner's
existing eight request-exception retries remain separate.

The profile declares `--max-context-tokens 1048064`, not the official model
context of 1048576. BEAM truncates chat content to `declared - max_tokens -
5000` with the bundled tokenizer, so the declared value moves the real prompt
about 1:1; 1048576 pushed the longest prompts to 1048452 and the endpoint
rejected the two longest chats with HTTP 400, while 1048064 keeps the longest
prompt at 1048045 and completes all 700 questions. A failed BEAM generation
step is `execution_invalid` and is retried, never scored as a model zero.

Formal `accept` requires clean child worktrees. `develop` permits local changes
but records tracked-diff and non-ignored-untracked fingerprints. Schema-v2
`run.json` also pins the content hash of the profile, requirements, suite
manifest, Stream snapshot, and locked fixtures. Resume and report reject drift.

## Concurrency

The ordered row contract, including the exact entrypoint and artifact boundary
for every K3 row, is maintained in `docs/k3-row-contracts.md`. The manifest's
`runner_fingerprint` is the executable copy of that review surface.

Suite manifests define the default concurrency for each runner. Only `develop`
supports an optional `--concurrency N` override for a development scenario.
`accept --acceptance simple|full` rejects `--concurrency` and uses profile values.
The develop override is applied to supported
runner flags (`-n`, `--max-connections`, and `--concurrency`) and is frozen as
`run.json.concurrency_override`; the generated redacted commands provide the
effective value used by each suite. Suites without a concurrency flag keep
their normal behavior. DeepSWE always uses committed task-job concurrency 113;
the generic concurrency override is ignored for DeepSWE. The value is frozen in
its checkpoint and runner fingerprint. Formal round/task attempts use mixed
the global task-job budget, and a task-local lock forbids same-task overlap.
Only after all 339 formal attempts complete does each round/task whose execution
never produced a binary reward receive up to one batch retry wave; a
completed reward-0 attempt is a scored model failure and is never retried. The Docker daemon keeps `bip=172.17.0.1/16` for its registry
proxy and allocates `/28` custom networks from `192.168.0.0/16`; a successful
410-network probe is the capacity gate for the task inventory. DeepSWE writes a
round/task/logical-attempt checkpoint before launching Pier. Resume reuses every
clear reward 0/1 result, fills only missing trials in a new shard of the same
logical attempt, and starts error-only retry waves only after all formal
round/task attempts have finished. Pause/stop selects Compose resources from
checkpoint-owned job paths.

## Service stability

All adapters use `novita_self_test.stability` as the single classifier:

| Class | Handling |
| --- | --- |
| `quota_exhausted` | `service_invalid`; stop requests; never score |
| `credential_invalid` | `service_invalid`; stop requests; never score |
| `rate_limited` / HTTP 429 | retry under the owning runner policy |
| `engine_overloaded` | retry under the owning runner policy |
| timeout / connection / HTTP 5xx | retry under the owning runner policy |
| explicit pass/fail | score only after a complete response exists |

If retries end with only stability errors, the result is `execution_invalid`,
not a model failure or score zero. Every suite result records failure-class
counts and recovery coverage. Pytest artifacts retain both the beginning and
end of long tracebacks so the terminal HTTP/API exception is not truncated.
BEAM treats a successful terminal completion with empty public content as a
valid model output worth zero, except for `abstention` questions, where an empty
or refusing answer may be correct and must still reach the judge; a request
error is execution-invalid and is never
sent to the judge. Judge calls use a global profile-pinned RPM, honor
`Retry-After`, and checkpoint only numeric scores. Judge 429, timeout, 5xx,
empty, or unparseable responses remain explicit retryable judge errors rather
than model zeros. Inspect never scores
`engine_overloaded` or unscored samples. deepSWE stops subsequent waves when a
trial reports credential or quota exhaustion.

## Results and completeness

K3 rerun ownership follows the committed official runner. `runner_owned` suites
are not wrapped by a second policy: Stream preserves `@flaky(reruns=2)`, Walle
preserves `--reruns 3 --reruns-delay 2`, K3 Features preserves its per-case
markers, Inspect preserves `retry_on_error=3`, and BEAM preserves its request
retry policy. Profiles use `any_pass` with one formal attempt plus at most three
batch reruns only where the official runner has no case-level policy: Image,
Params, and Prompt Tokens. deepSWE uses `error_only`: only an execution-invalid
round/task is retried, and a completed reward-0 result is final. HTTP 429, `engine_overloaded`, network,
timeout, environment, and unscored results are always retryable within the
owning policy. DeepSWE retries only incomplete/error task results, never clean
reward-zero results. All attempts remain evidence. BEAM request errors are
execution-invalid and never reach the judge; successful terminal empty vendor
responses are valid model outputs fixed at zero.

Statuses are `passed`, `failed`, `blocked`, `not_run`, `not_implemented`, and
`deferred` for suite results. Run lifecycle additionally uses `running`,
`pausing`, `paused`, and `interrupted`. Validity is independently `valid`,
`service_invalid`, or `execution_invalid`.

Every adapter writes one `suite-input.json` and one `suite-result.json`. The
input artifact freezes non-secret service/model/selector/runner parameters and
the final result reference. A result that claims `passed` is
rejected if expected cases/spec items/counts/tasks/attempts/trials or required
artifacts are incomplete. Skipped official cases cannot be silently counted as
passes unless the committed source contract explicitly makes the test file and
its skip behavior authoritative. K3 Stream p0/p1/p2 has that explicit policy:
items absent from the official self-validation file default to passed. Current
requirements revision and repo pins are frozen in `run.json`.

For delivery, every run materializes `test-artifacts/` beside `run.json`.
`bundle.json` records the run verdict, official/supplementary row counts, the
profile family, the observed `served_model`, the run `started_at`, and
per-file source/destination, byte size, SHA-256, and missing, excluded, or
secret-skipped counts. Each numbered suite directory contains
`suite-input.json`, `suite-result.json`, `suite-manifest.json`, and every
readable task/case/evidence artifact that the result references.

Evidence copying is allowlisted to readable structured formats (`.json`,
`.jsonl`, `.log`, `.md`, `.txt`, `.csv`, `.tsv`, `.yaml`, `.sh`). Bulk machine
formats stay on the execution host: a single DeepSWE acceptance produces about
1.6 GB of `.eval` and `.body` payloads that no reviewer reads. Excluded files
are recorded with their source path, byte size, and
`skipped: non_deliverable_artifact`, so the server-side original stays
traceable, and exclusion is policy rather than breakage: it never turns a run
`partial`. `summary.excluded_artifact_count` and `excluded_artifact_bytes`
report the withheld volume.

`run.json` also records the observed `served_model`. The profile family alone is
not the scored system, because one K3 catalogue serves both
`moonshotai/kimi-k3-h` and `kimi-k3-flash`; results for the two must never be
consolidated. `nvst collect` walks the committed runs, resolves the observed
served model (from `run.json` when present, otherwise from the suites' own
`service.served_model`), orders each deployment's runs by `started_at`, and
writes `index.json` plus a readable `index.md`. A run that served more than one
deployment is flagged rather than merged. With `--copy-bundles` it stages each
run's `test-artifacts/` directory under `<served-model>/<started-at>-<run-id>/`
so a submitted package follows the same model × time axis as the index. Row
order inside a run still comes from that run's own suite directories, so the
index never recreates the test matrix.

`suite-manifest.json` also records the effective
`parameters` (selector, rows, acceptance mode, concurrency, rounds, retry
policy, timeout, dataset/fixture identity, model/think mode, redacted command)
and normalized `dependencies` (expected pins versus observed runtime versions
for external tools such as Harbor, Terminal Bench 2, Kimi CLI, uv, Pier,
DeepSWE, the Code Bench runner, and both repository commits). Observed values
come only from runtime evidence such as `local-preflight.json` or
`archive/runtime-versions.json`; unresolved observed values stay `null` rather
than echoing the pin. Evidence outside the run tree is copied under
`artifacts/external/`. A missing source makes the bundle `partial` and is
reported; it is never silently omitted. A non-deliverable format is recorded as
excluded and does not affect completeness. Secret-bearing filenames or contents
are recorded as skipped and are not copied. `K3-CB` remains in the bundle and
report as supplementary evidence while `official_row_count` remains the count
of official rows only.

Repeated pytest suites declare their round count in the model manifest. Rows
3..6 use case-count concurrency per repeat and a barrier before the next
repeat. Each round keeps independent artifacts and original completeness counts. Runner-owned
reruns execute exactly as declared by official pytest markers or arguments.
For Params and Prompt Tokens, which have no official case rerun policy, up to
three profile-owned batch any-pass waves rerun failed or invalid cases after the
formal wave. K3 Rows 3..6 currently require 10 rounds each. Row 18 is an official K3
feature item and runs after the feature group. Required K3 precision acceptance
runs Rows 7..15 and 17 before the feature group. BEAM Row 16 and extended Kimi
Code Bench are optional precision suites selected explicitly when needed.
It uses the committed invalid/truncated toolcall recovery fixtures. Case A
replays 143 of 157 messages and requires non-empty valid JSON arguments that do
not repeat the sample forbidden pattern; the official judge does not enforce a
tool name. Case B replays 19 of 21 messages and accepts `WriteFile` or
`SetTodoList`. Streaming aggregation follows the Moonshot contract by collecting
all deltas per `tool_calls[].index`; the runner judges index 0 to match the official
non-stream branch. Each case has three independent streaming attempts, and
empty or invalid arguments are valid model failures only when the service
response itself is complete.

Inspect benchmark tolerance mode is profile-owned. K3 uses `lower_bound`: a
score fails only when it is more than the configured tolerance below the lowest
official observation. Positive differences, including differences greater than
the tolerance, do not fail. Reports retain the signed difference for review.

Replay writes `response.body`, a safe response-header map, and status/body hash
metadata for every case. These files are included in `suite-result.json`, so a
failed develop diagnosis points directly to the preserved response. Runtime
schema validation rejects unknown or missing run/result fields before use.

## Reports and exits

`acceptance.json` and `acceptance.md` are generated by the shared profile-driven
reporting layer. They contain every row declared by the active profile in
committed order, including Row 18 where that profile has it, plus separately
declared optional or supplemental diagnostics. K3 and K3-Flash share this
report structure; their profiles provide different rows, thresholds, and suite
inputs. Optional suites are shown as `not_run` when they are outside the
selected acceptance mode and do not gate the required verdict. Each row includes
status, validity, actual/official values, difference, runner pin, artifacts,
and non-run reason. A normal acceptance report never combines prior runs.
An explicit acceptance session may combine attempts only when service,
requirements, per-suite input, and applicable judge-access hashes match; profile
changes require a reviewed migration approval, and all attempt provenance is
retained in `acceptance-session.json`. Large raw evidence may remain on the
execution host; compact delivery reports use an evidence index rather than
copying intermediate artifacts. Doctor, managed control, DeepSWE
checkpoint, acceptance session, contract approval, BEAM judge checkpoint,
run snapshot, and run event artifacts are schema-validated.

Final verdicts are `passed`, `failed`, `inconclusive`, or `service_invalid`.
`failed` is reserved for complete valid model evidence below contract;
execution-invalid or missing evidence is `inconclusive`.

Exit 0 means `passed`; 1 means complete execution with verdict `failed`; 2 means
`inconclusive`, `service_invalid`, or a configuration/contract error; 3 means
human interruption. Exit 0 and 1 always have complete reports.

## Requirements refresh

`nvst requirements refresh --model <model> --doc <url-or-token>` fetches the
Docx with user identity, emits a structured source/attachment diff, and leaves
committed files unchanged. `--apply` updates the source token, revision, fetch
time, content digest, attachment lock, and snapshot revision. Signed attachment
URLs are discarded. It never labels structured rows unchanged merely because
`rows.yaml` was not regenerated; changed or unpinned table content blocks apply.

Profiles with a separate Stream document fetch both sources. Refresh reports a
structured acceptance-table digest and the executable Stream snapshot digest.
Changed table rows require the exact reviewed `--accept-table-sha` and an updated
`rows.yaml`; a changed Stream executable requires the corresponding local
snapshot before `--apply`.

# kimi-vendor-harness

This repository owns Novita model profiles, test selection, orchestration,
adapters, diagnosis, reporting, Novita API tests, the Code Bench suite, and the
DeepSWE adapter. Official Moonshot verifiers live under `vendor/` and are
read-only.

- Treat `models/<model>/profile.yaml`, `requirements/`, `suite-manifest.yaml`, and the
  shared K3-family test sets in `suites/k3-family.yaml` as the only executable matrix.
  A test set used by every K3-family model is defined once in `suites/k3-family.yaml`;
  a model manifest only names the sets it runs (`use`), its official rows and selectors,
  and the values where that model differs (thresholds, budgets). Changing a shared set
  changes it for every model. Do not branch on model names in Python to recreate
  profile policy.
- `kimi-k3-flash` is a separate profile under `models/kimi-k3-flash/`. It is
  pinned to Moonshot's K3-flash document revision 122, uses only its committed
  local rows, and reuses the DeepSWE adapter.
  Kimi Code Bench and Moonshot stress testing remain external closed gates.
- `vendor/kvv-k3` is a read-only snapshot of the official shared
  `MoonshotAI/kvv-k3` `k3` admission library (see `vendor/kvv-k3/VENDORED.md`). Its pinned source is shared by the Kimi K3, Kimi K3
  Flash, and Kimi K3.1 model family; all K3-family KVV-derived suites execute
  from this snapshot. Its three benchmark families are max-only, BEAM is
  excluded, and Git LFS fixtures must be materialized before execution.
- Run endpoints locally through `scripts/kvh` (or `uv run nvst ... --service-access`).
- Dependency packs are per-user/per-host. Use `scripts/setup-pack.sh` and `scripts/check-packs.py`; never assume a shared build host, shared cache, or shared long-test runtime.
- Execute the shared K3-family verifier from `vendor/kvv-k3`. Never copy or
  edit official verifier tests here.
- `suites/novita-api/`, `suites/kimi-code-bench/`, and `suites/deepswe/` are
  writable Novita-owned source. Preserve the imported subtree ancestry.
- Keep Pier and the 113-task public deepSWE corpus external at
  the path in `NVST_DEEPSWE_ROOT` on a prepared host. deepSWE is local-only and must not
  add an SSH fallback.
- Generated evidence belongs only in ignored `runs/`. A pass requires current,
  complete artifacts for the run's pinned requirements revision. Walle's
  `tool-call-schema-report.json` is optional; `cases.jsonl` plus exact pytest
  counts are the completeness boundary.
- Persist endpoint/judge secrets only in the private registry documented by
  `docs/credentials.md` (mode 0600), never in this repository. No run artifact,
  report, or log may persist Authorization. Access contracts name an
  environment variable only.
- Service stability classification is shared across adapters. Quota/balance and
  authentication failures are `service_invalid` stop conditions. HTTP 429,
  `engine_overloaded`, timeout, connection, and 5xx responses follow the
  official/profile rerun owner; if they never yield a complete result they are
  `execution_invalid`, never a model failure or score zero. Supplementary Row 18
uses the same rule: service errors are separated from valid model failures and
its two toolcall recovery cases must produce non-empty valid JSON arguments.
  A post-verifier `NonZeroAgentExitCodeError` with verifier reward one is
  retained as a warning but remains execution-valid; reward-zero executions
  with an agent exception remain invalid.
- `develop` generates a diagnosis handoff and never edits source automatically.
- `accept --acceptance simple|full|precision-core|k3-sop` stops immediately after any failed suite
  and publishes one verdict; remaining suites are recorded as blocked evidence.
  `precision-core` is the scoped required Rows 7..9 plus DeepSWE precision
  mode and is not a full acceptance. BEAM and Code Bench are optional explicit
  add-ons.
- Formal `accept` rejects a dirty working tree. `develop` records dirty
  content fingerprints, and new runs pin every executable profile input by hash.
- Replay pytest cases retain secret-safe response body, safe response headers,
  and status metadata under the suite artifact directory, including failures.
- K3 Stream p0/p1/p2 run the exact Moonshot Docx revision 244 single-file snapshot;
  its code and skip behavior are authoritative, and uncovered spec prose does
  not create extra executable gaps.
- K3 rows 3..6 define 10 independent pytest rounds with case-count concurrency
  and a barrier between repeats. Preserve official reruns exactly: Walle's CLI
  reruns and K3 Features markers are runner-owned; Params and Prompt Tokens use
  profile-owned batch any-pass only because they declare no official rerun.
  Preserve per-round counts and artifacts.
- K3 required execution order is rows 7..9 toolcall thinking effort, rows
  10..15 benchmark precision, row 17 DeepSWE, then feature rows 1.0..6 and 18.
  Row 16 BEAM and extended Kimi Code Bench are optional explicit suites and do
  not gate the default acceptance plan.
- K3 `k3-sop` follows Moonshot's pinned K3 上线 SOP revision 9: rows 7..9,
  10..12, 13..15, then rows 5, 6, 3, 4, Streaming P0/P1, and current
  Novita-owned Image, BEAM, DeepSWE, Row 18, and extended Code Bench coverage.
  Streaming P2 is intentionally excluded from this mode.
- K3 Inspect benchmark tolerances are one-sided lower bounds: fail only when a
  score is more than 2pp below the lowest official observation. Higher scores
  pass.
- BEAM generation completes all vendor requests before its judge starts. The
  judge runs in the background while the next suite executes and is awaited at
  run finalization; generation and judge artifacts are both required.
- Code Bench fixes global concurrency to the selected task count and runs
  serialized per-task pipelines over three baseline rounds: each task's next
  round starts as soon as that task completes, with no barrier between rounds.
  Its task-local
  concurrency is one. Other profile concurrency values remain defaults and may
  use `--concurrency N` in develop only; accept has no override. DeepSWE always
  uses the committed 113 task-job concurrency; the generic concurrency override
  is ignored for DeepSWE. Each Pier job runs
  one task; formal round/task attempts are mixed across tasks under that
  budget with a task-local lock forbidding same-task overlap, and after all
  339 formal attempts each round/task whose execution never produced a binary
  reward receives up to one batch retry wave. A completed reward=0 result is
  a scored model failure and is never retried. Docker keeps
  `bip=172.17.0.1/16`, allocates `/28` custom networks from
  `192.168.0.0/16`, and requires a 410-network capacity probe.
- K3 BEAM stores only its judge URL/model/API-key env name and profile-pinned
  global request rate. Endpoint and judge access metadata remain separate, but
  an explicitly configured provider credential may be shared by both roles.
  Successful terminal empty vendor responses score zero, except empty
  `abstention` answers, which still reach the judge;
  vendor request errors and judge errors are execution-invalid. Numeric judge
  scores are resumable checkpoints; no judge failure may silently become zero.
  K3 deepSWE uses
  `transport: local` on the prepared host and must pass the
  commit/worktree/Pier/task-count preflight before staging or execution.

- Managed runs use a transient user-systemd service, one-shot 0600 credential
  FIFO, start/status/pause/resume/stop, heartbeat, and append-only events. They
  never persist Authorization. Process-group fallback is explicit only.
  DeepSWE resume is
  round/task/logical-attempt aware and cleanup is limited to checkpoint-owned
  Compose resources. Explicit acceptance sessions may reuse evidence only when
  service, requirements, and per-suite input hashes match; contract drift needs
  an approval naming affected suites.

Run `uv run nvst list --model kimi-k3` for the active profile surface. Do not
push, merge, or delete run evidence without human approval. Run
`uv run pytest -q tests` before proposing changes.

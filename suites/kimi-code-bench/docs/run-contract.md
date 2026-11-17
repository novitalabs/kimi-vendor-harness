# Novita Kimi Code Bench Contract

## Scope

Novita Kimi Code Bench is the only supported Code Bench profile. New contracts
must set:

```json
{
  "runner": {"root": "runner/v2"},
  "execution_profile": {
    "kind": "official_observed",
    "schedule": "task_pipelines",
    "concurrency_scope": "task_count",
    "per_task_concurrency": 1,
    "recovery": {"enabled": true, "attempts_per_abnormal_task": 1},
    "harbor_n_attempts": 1,
    "harbor_timeout_multipliers": {
      "timeout_multiplier": 1.0,
      "agent_timeout_multiplier": 10.0,
      "verifier_timeout_multiplier": 10.0,
      "agent_setup_timeout_multiplier": 1.0,
      "environment_build_timeout_multiplier": 10.0
    },
    "kimi_loop_control": {
      "max_retries_per_step": 10,
      "max_steps_per_run": 500
    }
  }
}
```

The `execution_profile.concurrency`, when present, must equal
`run.concurrency`. `run.concurrency` must equal the selected task count when
`concurrency_scope` is `task_count`. The service must have
`preserve_thinking: true`.

## Run Contract

Create the task-scoped `run-contract.json` before any endpoint-consuming or
remote action. It must declare:

- a unique tag, detached mode, immutable evidence/status paths, selected task
  count, fixed denominator, three rounds, three attempts per task, and total
  baseline trial count;
- SHA-256 fingerprints for every behavior-owning V2 runner file, plus markers
  that prove the serialized per-task pipelines, bounded image pull, recovery queue, detached
  markers, and snapshot-aware fetch;
- the built-in `extended` or explicit `smoke` selection, Kimi CLI ref, Harbor
  version, Terminal Bench 2 ref, endpoint, model, non-secret headers,
  authentication source, remote streaming preflight, container proxy,
  versioned runtime-cache root, and isolated remote workdir;
- duplicate identity fields and `stop-and-inspect` behavior; and
- the secret policy for rendered files, logs, archives, reports, command
  arguments, and remote credential cleanup.

The timeout and retry objects above are exact values, not user-tunable defaults.
Contract validation rejects a missing or changed value so every invocation uses
the observed official settings.

`extended` is exactly 89 tasks and 267 baseline trials. `smoke` is exactly 15
tasks and 45 baseline trials. A changed runner fingerprint requires a new
contract; never alter a contract belonging to a started or paused run.

## Scheduling And Recovery

Baseline scheduling runs serialized per-task pipelines over the `(task, round)`
queue. Every selected task starts its first round immediately (stored as
artifact round 1); a task that finishes a round starts its next round at once,
without waiting for other tasks. Global
concurrency is fixed to the selected task count, and `per_task_concurrency=1`
prevents the rounds of one task from overlapping. Do not pre-pull every
image.

After all three baseline rounds complete, classify each task:

- retry up to three times when the task has an execution-invalid observation: incomplete
  stream, timeout or process error before verifier completion, MCP failure,
  environment failure, verifier failure, or missing verifier reward;
- a reward-one verifier outcome is authoritative for a successful task. If Harbor
  records `NonZeroAgentExitCodeError` only after the agent has finished and the
  verifier has completed with reward one, keep the exception as a diagnostic
  warning but mark the trial execution-valid. Reward-zero executions with an
  agent exception remain execution-invalid and follow recovery;
- do not retry a clean reward-zero observation; and
- retain both baseline and recovery leaves.

An explicit human pause, runner failure, credential failure, or security issue
  may stop the owned run. Preserve partial artifacts and mark the result
incomplete. A single task error does not stop the queue.

Use `kbench stop <run-directory>` for an explicit human stop of a detached
run. The command validates the immutable local/remote binding before signalling
the exact remote process group. A successful stop is `paused`, not a benchmark
failure, and remains non-publishable. Each Harbor invocation uses one internal
attempt and timeout multipliers `1/10/10/1/10` for general, agent, verifier,
agent setup, and environment build. Kimi independently retries a failed model
step up to 10 times and permits up to 500 steps per run.

## Evidence And Security

Only the contract-named environment variable supplies a static runtime key.
The value is never stored in the contract, rendered files, logs, archives,
reports, or command arguments. Run the secret audit before remote push and
after archive extraction.

The runner writes `config.snapshot.json` before launch. It records the endpoint,
model, headers, task list, task counts, scheduling/recovery settings, timeout
multipliers, Kimi loop control, runner contract hash, authentication source,
and exact remote workdir. It must match the contract before publication.

The run root also contains its immutable `run-contract.json`, release manifest,
rendered runner inputs, gate receipts, and atomically updated `status.json`.
Remote setup validates a real streaming tool call through the production
recording path and saves secret-free preflight evidence. Runtime revisions for
Kimi CLI, Harbor, and Terminal Bench 2 are checked against the contract and
saved in `runtime-versions.json`.

Runtime reproducibility is validation-based, not reinstall-based. Kimi CLI
source and wheel are keyed by commit, Terminal Bench 2 source is keyed by
commit, and Harbor is keyed by its version plus Python minor version. A cache
miss is populated under a file lock and atomically renamed only after all
checks pass. Invalid cache entries are moved aside for diagnosis. A run uses a
separate Terminal Bench worktree so task execution cannot mutate the shared
source cache. The uv builder version is also pinned. Never use a global
`uv tool install --force` as a per-run version check.

Every archived trial must satisfy the observed artifact contract:

- raw `llmproxy/openai` request and response evidence;
- nonempty, mutually consistent proxy `job_id`, `attempt_id`, `container_id`,
  `route_path`, and derived `proxy_session_id`;
- explicit local provenance, target URL components, response completeness,
  disconnect state, and socket-arrival journals for recording schema v2;
- agent stream and session evidence, verifier result, termination classification,
  AgentGym/Mangrove compatibility files, and an `exception.txt` for abnormal
  termination; and
- parseable `agent/.kimi/mcp.json` with an object `mcpServers`, including an
  empty object when the task has no MCP server.

## Aggregation

Every selected task requires three baseline trial records, one for each observed
round. Exceptions and missing verifier rewards are zero-score observations;
missing baseline records block publication rather than becoming synthetic zeroes.

For each baseline round, divide passed tasks by the fixed task denominator. The
formal final accuracy is the arithmetic mean of those three round scores.
Recovery is retained for abnormal-execution diagnosis and must not replace or
raise a baseline round score. Any-pass, recovery-pass, and trial-level rates are
diagnostics only. Smoke results remain 15-task diagnostic evidence and are not
an extended benchmark score.

After fetch, `kbench finalize` is mandatory for publication. It validates every
leaf, produces triage, metrics, E2E, and 5-second ITL reports, validates the
terminal status and runtime-cache evidence, and writes the singular
`finalization-manifest.json` receipt.

## Stable Release

`runner/v2` is released as `novita-kimi-code-bench-v1.1.0`. The machine-readable
identity is `runner/v2/release-manifest.json`; `kbench --version` returns the
runner version. `scripts/validate-release.sh` is the reproducible local release
gate. See `CHANGELOG.md` and `RELEASE.md` for the frozen surface and qualification
evidence.

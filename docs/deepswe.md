# DeepSWE Row 17 Contract

## Official workload

Row 17 runs the public 113-task DeepSWE corpus through Pier with Kimi Code
`0.29.0`, thinking effort `max`, and three scored rounds. Official comparison is
`66.7 avg@3` with context window 1048576.

The committed profile fixes:

- runtime root `/srv/kvh/deepswe`;
- deep-swe commit `435ee89ec2f2e2289f33b0da4f992f0b7b7266b9`;
- Pier `0.3.1`;
- 113 tasks;
- Kimi Code `0.29.0`;
- three rounds;
- global task-job concurrency 113; the generic `--concurrency` override is
  ignored for DeepSWE;
- mixed round/task scheduling of all 339 formal (round, task) attempts under
  the global task-job budget, with a task-local lock forbidding same-task
  overlap;
- after all formal attempts, up to one batch retry wave for each round/task
  whose execution never produced a binary reward; a complete attempt with
  reward 0 is a scored model failure and is never rerun.

## Ownership and preflight

`suites/deepswe/pier_kimi/` is the Novita-owned Pier agent.
The corpus, Pier environment, jobs, task images, and verifier stay outside the
repository on the prepared execution host. There is no SSH or copied-corpus fallback.

Before any mutation or trial, preflight verifies runtime commit, clean tracked
worktree, Pier version, task inventory, execution site, endpoint header support,
and the committed profile. A mismatch writes a structured blocker and starts no
Pier job.

## Managed execution

Formal Row 17 is started only through managed nvst execution:

```bash
scripts/kvh --base-url https://endpoint.example/v1 --served-model kimi-k3 -- \
  develop --model kimi-k3 --select 17
```

`scripts/kvh` resolves endpoint Authorization from `KIMI_API_KEY` or the private
credential registry by URL. The managed systemd unit supplies it through a one-shot FIFO.
No direct DeepSWE shell runner is supported.

Each `(round, task, logical attempt)` is an independent Pier job with
`--n-concurrent 1`. Formal attempts of every round and task are mixed under
the global task-job budget: a task that finishes one formal round starts its
next formal round immediately, without waiting for other tasks. A task-local
lock holds for the complete Pier/Harbor lifecycle, which forbids the same
task's rounds from running at the same time. Only after all 339 formal
attempts complete do round/tasks whose execution never produced a binary reward
receive up to one batch retry wave; a completed reward-0 result is final.
`deepswe-checkpoint.json` is
written before launch and records round, task, logical attempt, part, global
concurrency, and process result.

Pause stops the managed cgroup and checkpoint-owned Compose resources. Resume:

- keeps every task result with explicit reward 0 or 1;
- retries only missing/unclear task results;
- keeps replacement work in the same logical attempt;
- does not start error-only retry waves until all 339 formal round/task attempts
  have finished.

Each trial starts a local request recorder inside its container. Kimi Code talks
to that recorder with a placeholder key; the recorder injects the runtime key
from a temporary file and writes request/response bodies (or hashes), status,
safe headers, request/trace IDs, and available replica/rank/route headers under
the trial's `logs/agent/request-evidence/`. The suite also emits
`task-results.jsonl` with verifier submetrics, agent steps/tokens, result paths,
and the evidence directory for every round/task attempt.

## Scoring

Each task result is binary. For a round/task:

- any reward 1 across formal plus allowed reruns is pass;
- a complete attempt with reward 0 (and no reward 1 anywhere) is not-pass;
- no binary reward after retries are exhausted is execution-invalid.

Only an execution-invalid round/task is retried; a completed reward-0 result is
final, so the reported score never measures the retry policy.

Every report carries the three outcomes side by side, as
`counts.round_task_passes`, `counts.round_task_not_passes`, and
`counts.round_task_exec_unknown` (mirrored under `coverage.outcome_split`, with
the per-round task lists under `coverage.exec_unknown_round_tasks`):

- `passed` — a reward 1;
- `model_not_pass` — a complete attempt with reward 0 and no reward 1 anywhere;
- `exec_unknown` — no binary reward from any physical attempt.

The two failure kinds are never merged. `model_not_pass` measures the model;
`exec_unknown` measures the run, and is reported as `execution_invalid` with
reason `deepswe_rounds_incomplete` rather than folded into the score.

For each round:

```text
round_score = passed_tasks / 113
```

Final score:

```text
avg@3 = (round_1_score + round_2_score + round_3_score) / 3
```

Physical reruns do not change the denominator. Partial, paused, missing, or
unclear evidence is `inconclusive`, never a model score of zero. Token totals
include every physical attempt.

## Artifacts

Required artifacts, matched by exact filename:

- `local-preflight.json` — the four pinned runtime observations;
- `deepswe-checkpoint.json` — the resumable (round, wave, part, task) ledger;
- `round-01-summary.json`, `round-02-summary.json`, `round-03-summary.json` —
  one per scored round, each carrying its own `score`, `passed_tasks`,
  `not_pass_tasks`, `exec_unknown_tasks`, and per-task results;
- `round-summary.json` — the aggregate: `round_scores`, `avg@3` as `score`,
  `outcome_split`, and every round summary;
- `task-results.jsonl` — one inspectable record per round/task, with verifier
  submetrics and the request-evidence directory;
- `adapter-result.json` — the normalized suite result.

Per-record Pier stdout/stderr logs, each trial's `result.json`, and its
`logs/agent/request-evidence/` directory are also recorded in
`artifact_paths`.

## Capacity

Each trial uses two Docker networks. The host keeps the registry bridge at
`172.17.0.1/16`, allocates custom `/28` networks from `192.168.0.0/16`, and
requires the 410-network create/cleanup gate. All required task/base images must
be present before formal launch. Scenario `--concurrency` overrides define the
global task-job concurrency; with 113 tasks, at most 113 jobs run simultaneously,
with same-task rounds serialized.

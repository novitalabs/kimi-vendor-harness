# Changelog

## 1.1.0 - 2026-09-10

- Run baseline trials as saturated per-task pipelines: each task advances to
  its next round immediately after finishing, removing the round barrier that
  left workers idle at every round tail. Rounds of one task remain serialized
  by the pipeline itself.
- Reduced baseline rounds from five to three; the published score is now the
  arithmetic mean of three baseline-round scores. Extended is 267 trials,
  smoke is 45.
- Renamed the execution schedule to `task_pipelines` and the task execution
  semantics token to `task-pipelines-with-recovery`; contracts carrying the
  previous `round_barrier_task_pipelines` schedule are rejected.

## 1.0.5 - 2026-09-09

- Treat a post-verifier `NonZeroAgentExitCodeError` with verifier reward one as
  execution-valid while retaining the process warning; reward-zero executions
  remain invalid.
- Use per-task pipeline workers so a completed task immediately advances to its
  next round without waiting behind another task's slow round.

## 1.0.4 - 2026-09-09

- Replaced per-task round handoff with round-barrier task pipelines.
- Fixed global benchmark concurrency to the selected task count while starting
  each task's next round immediately after its previous round completes.
- Added contract, snapshot, release, and regression coverage for the schedule.

## 1.0.3 - 2026-09-09

- Added the immutable `per_task_concurrency=1` contract field.
- Serialized rounds behind a round barrier while retaining task-count
  concurrency across tasks within each round.
- Added snapshot/artifact validation and regression coverage for the task-local
  concurrency boundary.

## 1.0.2 - 2026-08-14

- Aligned all five Harbor timeout multipliers with the supplied official
  artifact: `1/10/10/1/10` for general, agent, verifier, agent setup, and
  environment build.
- Explicitly configured Kimi `max_retries_per_step=10` and
  `max_steps_per_run=500` instead of inheriting the CLI defaults.
- Fixed each Harbor trial to one internal attempt while retaining one
  post-baseline recovery observation per execution-invalid task.
- Added immutable contract, snapshot, rendered-command, artifact, and release
  gates for the timeout and retry settings.
- Kept the two local schema-v2 agent-stream files mandatory for new leaves while
  allowing the supplied historical official leaf to validate without them.

## 1.0.1 - 2026-08-14

- Added a bounded recording-proxy drain for requests still in flight when Kimi
  CLI exits.
- Made a drain timeout fail the agent execution instead of leaving a clean leaf
  with unmatched request and response evidence.
- Added concurrent integration coverage for successful drain and drain timeout.

## 1.0.0 - 2026-08-14

First stable release of the Novita Kimi Code Bench runner.

- Froze `official_observed` as the only execution profile.
- Froze the 89-task extended and 15-task diagnostic smoke selections.
- Implemented five independent baseline rounds with a barrier between rounds.
- Limited recovery to three reruns per execution-invalid task; clean reward-zero
  trials remain baseline observations and are never retried for score.
- Published only the arithmetic mean of the five baseline-round scores.
- Added the real per-trial recording proxy, raw OpenAI request/SSE evidence,
  stable local runner identities, MCP evidence, artifact diff and validation.
- Added versioned, validated runtime reuse for Kimi CLI source/wheel, Terminal
  Bench 2 source, Harbor/Python, and uv while keeping run worktrees and task
  containers isolated.
- Added immutable run contracts, status, runtime evidence, finalization
  manifests, secret gates, E2E diagnostics, and SSE arrival/ITL diagnostics.
- Added `kbench stop` with explicit `paused` state for human-stopped detached
  runs.
- Made recording-proxy JSON metadata updates atomic.

This release claims observed behavior and artifact compatibility with the
supplied official sample. It does not claim source-level parity with an
unpublished official implementation.

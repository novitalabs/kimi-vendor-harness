# Novita Kimi Code Bench v1.1.0

## Release Decision

`v1.1.0` is the stable local runner for the observed Novita Kimi Code Bench
contract. `runner/v2` is the only supported runner and
`official_observed` is the only supported execution profile.

The stable compatibility boundary includes:

- the existing 89-task extended and 15-task diagnostic smoke selections;
- three baseline rounds per task, and up to three recovery
  observations for each abnormal task with
  an execution-invalid baseline trial;
- serialized per-task pipelines: a task that finishes a round immediately
  starts its next round, with no barrier between rounds and no overlap between
  rounds of one task;
- global concurrency fixed to the selected task count and task-local
  concurrency fixed to one;
- reward-one verifier outcomes are authoritative when a post-verifier agent
  cleanup warning accompanies them;
- round-by-round scoring followed by the arithmetic mean of three rounds;
- real per-trial request/SSE recording and the observed artifact file set;
- immutable task-scoped run roots, runtime/source pins, secret gates, status,
  finalization, E2E, and ITL evidence; and
- validated runtime-cache reuse without sharing mutable run or container state.

This minor release makes the scheduler match the documented per-task pipeline
contract: baseline trials of different tasks mix freely under the global
concurrency budget, while each task's rounds stay strictly sequential. It uses
three baseline rounds with up to three abnormal-task recovery attempts.
Every new contract now requires one Harbor attempt
per trial, timeout multipliers
`1/10/10/1/10`, Kimi `max_retries_per_step=10`,
`max_steps_per_run=500`, and `per_task_concurrency=1`. The same values are copied into the immutable snapshot,
while timeout/retry values are also copied into the rendered Harbor command and
redacted effective Kimi configuration.

## Qualification Evidence

The release candidate passed the complete local release gate, including exact
contract rejection tests, snapshot/render tests, template syntax checks,
recording-proxy integration tests, and all existing artifact, metrics, status,
cache, E2E, and ITL tests.

The earlier pre-release diagnostic smoke
`k26-official-observed-v1-smoke-20260814-v2` completed 21 of 75 baseline trials
before an explicit human stop: 18 clean passes, 3 clean reward-zero results,
and no execution-invalid result. The first complete archived leaf passed the
artifact validator and secret audit. This partial smoke qualifies architecture
and artifact flow only; it is not a benchmark score. The final P0 hardening for
human-stop classification was completed after that run and is covered by the
release tests; it was not rerun end to end at the human's request. That run
predates the v1.0.2 timeout/retry correction and is not evidence that the new
values reached a live Harbor leaf.

Run the reproducible local release gate with:

```bash
./scripts/validate-release.sh
```

The workspace Skill validator is a separate repository-level gate and must use
the dedicated Skill tooling environment declared by `AGENTS.md`.

## Compatibility Limits

- The official framework source remains unpublished, so source-level parity is
  not claimed.
- Task and MCP behavior are the pinned local Terminal Bench 2 behavior.
- Gateway-shaped IDs are stable local-runner identities, not vendor-issued
  gateway IDs.
- Proxy arrival timing is a socket-read upper bound, not exact token-level ITL.
- A smoke run is diagnostic evidence and must never be reported as an 89-task
  extended Code Bench score.

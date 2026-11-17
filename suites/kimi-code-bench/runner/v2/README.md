# Novita Kimi Code Bench runner V2

Stable release: `v1.1.0`. Query it with `kbench --version` and validate the
local release with `../../scripts/validate-release.sh`.

This is the only active runner. It accepts `execution_profile.kind=official_observed`
and implements Terminal Bench 2/Harbor three-round serialized per-task
pipelines with abnormal task recovery. Each task advances to its next round as
soon as it finishes, and rounds of one task never overlap. The runner has no presets or user
credential configuration.

The implementation is intentionally split into evidence operations (`artifact-*`,
`triage`) and the remote execution operation (`run`). A run contract supplies all
service, task, pin, and remote identity; authentication is resolved only from the
declared environment variable.

`run` creates a self-contained immutable evidence root, performs a real remote
streaming tool-call preflight through the recording and container proxies,
checks all runtime pins, and launches the detached per-task pipelines. Runtime objects
are loaded from a shared pin-keyed cache. Warm runs validate and reuse them;
cold runs build under a lock and atomically publish only a valid result. Harbor
uses a versioned relocatable venv, Kimi CLI uses a commit-keyed wheel, and each
run gets an isolated worktree from the pinned Terminal Bench cache. `tail` refreshes
the local atomic `status.json` from `.kbench-running`, `.kbench-done`,
`.kbench-paused`, or `.kbench-failed` plus remote trial evidence. `fetch` uses
only the run-root
contract and snapshot, verifies their hash binding, and extracts the archive.

The publication path is:

```bash
kbench run <run-contract.json> --run-dir <evidence-dir>
kbench tail <evidence-dir>
kbench stop <evidence-dir>
kbench fetch <evidence-dir>
kbench finalize <evidence-dir>
```

`finalize` runs the post-fetch contract/snapshot/secret gate, validates all
trial leaves, writes triage, round-mean metrics, E2E and ITL reports, validates
final status, and emits `finalization-manifest.json`. The copied
`release-manifest.json` identifies runner v1.1.0 and explicitly limits the claim
to behavior and artifact parity observed from the supplied official sample.

`stop` is the explicit-human-stop path for detached runs. It verifies the
contract/snapshot binding and the remote PID's workdir and command before
signalling that run's process group. The remote trap cleans the temporary key
and leaves a `paused` status. Paused evidence is intentionally not publishable.

The post-fetch gate requires `runtime-versions.json` to match the contract's
Kimi CLI/TB2 commits, Harbor/Python and uv versions, runtime-cache root, and a
valid Kimi wheel SHA-256. Cache-hit fields are evidence, not score inputs.

Post-run diagnostics are also read-only evidence operations:

```bash
kbench itl <archive-or-trial> --threshold-ms 5000
kbench e2e-phases <archive> --high-step-threshold 100
```

ITL fallback timing is a socket-read upper bound, not token-level ITL.
`task_total_e2e_ms` is Harbor's complete trial wall time;
`agent_execution_e2e_ms`/`agent_execution_ms` cover only Harbor's agent phase.
The observed Harbor invocation makes one internal attempt and uses timeout
multipliers `1/10/10/1/10` for general, agent, verifier, agent setup, and
environment build. Kimi uses `max_retries_per_step=10` and
`max_steps_per_run=500`. Long task execution inside the declared agent window is
not runtime-cache setup time.

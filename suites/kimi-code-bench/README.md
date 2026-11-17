# Novita Kimi Code Bench

Local-first Code Bench runner for Kimi endpoint evaluation.
The repository implements the observed Novita Code Bench execution and artifact
contract inferred from the supplied official artifact. It does not claim
source-level parity with an unpublished official implementation.

Stable release: `v1.1.0`.

## Supported Contract

- `runner/v2` is the only runner.
- `execution_profile.kind=official_observed` is the only execution profile.
- `extended` is the fixed 89-task, three-round evaluation.
- `smoke` is the fixed 15-task, three-round diagnostic evaluation.
- Baseline trials run as serialized per-task pipelines: each task advances
  through its rounds as soon as it finishes, without a barrier between
  rounds, and rounds of one task never overlap.
- Global concurrency is fixed to the selected task count and
  `per_task_concurrency=1` prevents rounds of one task from overlapping.
- Only execution-invalid tasks receive up to three recovery observations. A reward-one
  verifier outcome remains execution-valid when an agent process cleanup
  warning is recorded after verifier completion; reward-zero executions with an
  agent exception remain invalid.
- The published score is the arithmetic mean of the three baseline-round
  scores; recovery never changes that score.
- Harbor uses the official observed timeout multipliers `1/10/10/1/10` for
  general, agent, verifier, agent setup, and environment build timeouts.
- Kimi uses `max_retries_per_step=10` and `max_steps_per_run=500`; one Harbor
  attempt is made per trial before abnormal-task recovery.

The benchmark host reuses validated, pin-keyed Kimi CLI, Terminal Bench 2,
Harbor/Python, and uv runtime objects. Every run still owns its worktree, jobs,
containers, status, recordings, and artifacts.

## CLI

```bash
./runner/v2/kbench --version
./runner/v2/kbench probe examples/run-contract.example.json
./runner/v2/kbench run <run-contract.json> --run-dir <evidence-dir>
./runner/v2/kbench tail <evidence-dir>
./runner/v2/kbench stop <evidence-dir>
./runner/v2/kbench fetch <evidence-dir>
./runner/v2/kbench finalize <evidence-dir>
```

Authentication is supplied only through the environment variable named by the
run contract. Never store a key in this repository, a contract, or an artifact.

Run the complete local release gate with:

```bash
./scripts/validate-release.sh
```

## Repository Layout

```text
runner/v2/      Versioned CLI and implementation
scripts/        Contract, artifact, status, secret, and release gates
examples/       Credential-free run/status examples
docs/           Run and observed artifact contracts
_workspace/     Ignored local run evidence
```

See [docs/run-contract.md](docs/run-contract.md),
[docs/observed-artifact-contract.md](docs/observed-artifact-contract.md), and
[RELEASE.md](RELEASE.md) for the frozen behavior and compatibility limits.

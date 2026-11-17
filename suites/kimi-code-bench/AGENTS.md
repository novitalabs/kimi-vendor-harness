# Repository Instructions

This directory is the vendored Novita Kimi Code Bench runner used by the
`kimi-code-bench` suite. Do not maintain a second implementation copy elsewhere.

- Keep `runner/v2` and `official_observed` as the stable v1 compatibility
  boundary. A behavior or artifact-contract change requires updated tests,
  runner fingerprints, release notes, and an appropriate version decision.
- Preserve the 89-task extended and 15-task smoke sets, three baseline rounds,
  serialized per-task pipelines, abnormal-only recovery, and baseline-round mean scoring.
- Code Bench global concurrency equals the selected task count; each task owns
  one sequential three-round pipeline, so a completed task advances immediately
  without allowing another round of that task to overlap.
- A reward-one verifier outcome is execution-valid when a
  `NonZeroAgentExitCodeError` is recorded only after verifier completion; keep
  the process warning as diagnostic evidence; reward-zero executions with an
  agent exception remain invalid and are eligible for recovery.
  unless the human explicitly changes the benchmark contract.
- Keep credentials out of source, contracts, logs, archives, and command
  arguments. Authentication is environment-variable based.
- Store run evidence under `_workspace/`; it is ignored by
  Git and must not be committed.
- Run `./scripts/validate-release.sh` before declaring a stable build.
- The standalone runner tests use `python3`.

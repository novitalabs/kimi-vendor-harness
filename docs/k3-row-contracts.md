# K3 Row Contracts

This document is the review surface for the production K3 matrix. The
manifest remains executable; each suite's `runner_fingerprint` carries the
same entrypoint, concurrency, retry-owner, retry-limit, retry-schedule, and
artifact contract so `nvst list` and run evidence expose the effective policy.

## Common Rules

- Every suite writes `suite-input.json` before execution and
  `suite-result.json` after execution. `stdout.log` and `stderr.log` are
  retained by the command adapter.
- A runner-owned retry policy is executed only by the named official runner.
  A profile-owned `any_pass` policy runs one formal attempt, then at most the
  declared batch reruns after the formal wave. Retryable service failures never
  become a model score.
- `serial_requests` means one request at a time. `per_repeat_case_count` means
  each repeat uses the declared case-count worker budget and the next repeat
  waits for the previous one. Scores and denominators are never changed by
  retries.

## Ordered Matrix

| Row | Entrypoint | Concurrency | Retry policy | Required result artifacts |
| --- | --- | --- | --- | --- |
| 1.0 | KVV pytest, `test_stream_spec.py`, `STREAM_SPEC_PRIORITY=P0` | 1 serial test process | `pytest-rerunfailures`, 2 reruns, immediate | `cases.jsonl`, `summary.json` |
| 1.1 | KVV pytest, `test_stream_spec.py`, `STREAM_SPEC_PRIORITY=P1` | 1 serial test process | `pytest-rerunfailures`, 2 reruns, immediate | `cases.jsonl`, `summary.json` |
| 1.2 | KVV pytest, `test_stream_spec.py`, `STREAM_SPEC_PRIORITY=P2` | 1 serial test process | `pytest-rerunfailures`, 2 reruns, immediate | `cases.jsonl`, `summary.json` |
| 2 | `novita-self-test` `replay_fixture.py image` | 1 serial request, 900s request timeout | profile batch any-pass, 3 reruns after formal | `run.json`, `summary.jsonl`, `summary.md`, `verdict.json`, per-case request/response files |
| 3 | KVV pytest, `tests/params` | 18 workers per repeat (case count); 10 repeats with a round barrier | profile batch any-pass, 3 reruns after formal | `repeat-summary.json`, per-repeat `cases.jsonl` and `summary.json` |
| 4 | KVV pytest, `tests/tool_call_json_schema` | 408 workers per repeat (case count); 10 repeats with a round barrier | pytest `--reruns 3 --reruns-delay 2`, immediate | `repeat-summary.json`, per-repeat `cases.jsonl` and `summary.json`; tool report optional |
| 5 | KVV pytest, `tests/k3_features` | 126 workers per repeat (case count); 10 repeats with a round barrier | official pytest markers, 5 reruns, immediate | `repeat-summary.json`, per-repeat `cases.jsonl` and `summary.json` |
| 6 | KVV pytest, `tests/prompt_tokens` | 59 workers per repeat (case count); 10 repeats with a round barrier | profile batch any-pass, 3 reruns after formal | `repeat-summary.json`, per-repeat `cases.jsonl` and `summary.json` |
| 7 | Novita Inspect artifact-reconstructed toolcall benchmark, effort low | 113 Inspect requests (task count) | Inspect `retry_on_error=3`, immediate | one `.eval` log, `stdout.log`, `stderr.log` |
| 8 | Same toolcall benchmark, effort high | 113 Inspect requests (task count) | Inspect `retry_on_error=3`, immediate | one `.eval` log, `stdout.log`, `stderr.log` |
| 9 | Same toolcall benchmark, effort max | 113 Inspect requests (task count) | Inspect `retry_on_error=3`, immediate | one `.eval` log, `stdout.log`, `stderr.log` |
| 10 | KVV Inspect `eval.py ocrbench`, effort low | 900 Inspect requests | Inspect `retry_on_error=3`, immediate | one `.eval` log, `stdout.log`, `stderr.log` |
| 11 | KVV Inspect `eval.py ocrbench`, effort high | 900 Inspect requests | Inspect `retry_on_error=3`, immediate | one `.eval` log, `stdout.log`, `stderr.log` |
| 12 | KVV Inspect `eval.py ocrbench`, effort max | 900 Inspect requests | Inspect `retry_on_error=3`, immediate | one `.eval` log, `stdout.log`, `stderr.log` |
| 13 | KVV Inspect `eval.py mmmu`, effort low | 900 Inspect requests | Inspect `retry_on_error=3`, immediate | one `.eval` log, `stdout.log`, `stderr.log` |
| 14 | KVV Inspect `eval.py mmmu`, effort high | 900 Inspect requests | Inspect `retry_on_error=3`, immediate | one `.eval` log, `stdout.log`, `stderr.log` |
| 15 | KVV Inspect `eval.py mmmu`, effort max | 900 Inspect requests | Inspect `retry_on_error=3`, immediate | one `.eval` log, `stdout.log`, `stderr.log` |
| 16 | Optional BEAM generation followed by background `nvst beam_judge_run` | generation 50; each invalid task gets up to 3 task retries; judge 50; judge rate 500 RPM | Runs only when Row 16 is explicitly selected; generation completes before the next vendor suite starts; invalid `repetition`/empty/error answers are retried per task, judge runs without vendor traffic, and finalization waits for the judge. | `answers.jsonl`, `generation-retry-summary.json`, retry input/output files, `generation-result.json`, `beam-judge-process.json`, `judge.stdout.log`, `judge.stderr.log`, `judge-answers.jsonl`, `judge-scores.jsonl`, `scores.jsonl`, `adapter-result.json` |
| 17 | Pier `deep-swe/tasks` via `pier_kimi.kimi_code:KimiCode` | global task-job concurrency is fixed at 113 and ignores the generic concurrency override; each Pier job runs one task | mixed round/task scheduling runs all 339 formal attempts under the fixed budget with a task-local lock forbidding same-task overlap; after all formal attempts, each round/task whose execution never produced a binary reward receives one batch retry wave, and a completed reward=0 result is a final model failure that is never retried | `local-preflight.json`, `deepswe-checkpoint.json`, `round-01-summary.json`, `round-02-summary.json`, `round-03-summary.json`, `round-summary.json`, `task-results.jsonl`, `adapter-result.json` |
| 18 | Novita `toolcall_arguments_recovery.py` | 1 serial request, 300s timeout; 3 formal attempts per case | no rerun wave; six independent case attempts | `case-a.json`, `case-b.json`, `adapter-result.json` |

`test_groups.optional_precision` contains the explicit optional suites. They
are available for development diagnosis or an additional operator-selected gate,
but are not included in the default required acceptance modes.

## Open Review Points

- Stream P0's known PPIO Gateway `MODEL_NOT_FOUND` error-envelope mismatch is
  the profile exception `test_stream_spec_prefill_error`: it remains visible as
  a failed case but does not stop the acceptance plan. Any additional unknown
  P0 failure still triggers global fail-fast.
- Rows 7..15 retain `.eval` as the scorer source. Per-sample HTTP request and
  response bodies are not currently duplicated outside Inspect's log; add a
  recording proxy only if the production evidence requirement demands raw wire
  replay for these rows.
- Row 16 vendor generation and judge calls are separate phases. A judge error
  must remain execution-invalid and must never become a zero vendor score.

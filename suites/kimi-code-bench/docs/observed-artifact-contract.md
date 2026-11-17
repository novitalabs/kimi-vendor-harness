# Observed Artifact Contract

This contract describes the evidence produced by the local runner after a
Harbor trial is archived. It is intentionally named `observed` because the
official Code Bench implementation is not open to this workspace; the shape is
derived from the supplied official artifact and is not a claim of source-level
parity.

Each trial gets two additional files:

- `trial-record.json`: complete redacted evidence for replay and comparison.
- `triage.json`: compact termination and request-integrity summary.

## Official File Set

For `execution_profile.kind=official_observed`, every archived leaf must retain
the following stable files. The local Harbor compatibility writer creates these
only when the source runner did not emit them, and marks synthetic content with
`identity_source=local_synthetic`:

```text
artifact.json
config.json
docker-compose.yaml
image-mounts.json
agentgym-meta.json
agentgym-compose-config/00.yaml
agentgym-compose-config/01.yaml
artifacts/manifest.json
result.json
trial.log
verifier/reward.txt
verifier/test-stdout.txt
agent/kimi-cli.txt
agent/.kimi/config.json.bak
agent/.kimi/config.toml
agent/.kimi/mcp.json
agent/agent_prepare/agent_prepare.log
llmproxy/openai/meta.json
mangrove-meta.json
```

New local recording-schema-v2 leaves additionally require
`agent/kimi-stream.jsonl` and `agent/execution-meta.json`. These local evidence
extensions are not required when validating the supplied historical official
leaf, whose proxy metadata predates `recording_schema_version`.

`llmproxy/openai/requests/*.json` and `llmproxy/openai/responses/` are required
capture locations, but their counts are task-dependent and must not be compared
as fixed constants. The request records, response metadata, response bodies,
`responses/*.arrival.jsonl` journals are authoritative proxy-side socket-read
timing for newly recorded runs. The response bodies and `[DONE]` markers remain
authoritative payload evidence. An `exception.txt` file is required for an
execution-invalid termination. A post-verifier `NonZeroAgentExitCodeError` may
also retain `exception.txt` as a non-fatal diagnostic warning when the verifier
completed with reward one; that trial remains execution-valid. The file
is not required for a clean reward-one or clean reward-zero trial.

Request and response JSON metadata are published with atomic replacement. The
proxy writes final response metadata before sending the terminal chunk to a
connected client, so readers never observe an empty or truncated JSON file and
a client that consumed the complete response can observe final metadata.
Before the trial proxy exits it stops accepting new model requests and drains
every accepted request for up to 120 seconds. A drain timeout makes the trial
execution-invalid; it must not be published as a clean leaf with unmatched
request and response evidence.

The observed identity must retain the real recording-proxy association when the
profile is official: `job_id`, `attempt_id`, `container_id`, `route_path`, and
the derived `proxy_session_id`. MCP configuration must always be present; an
empty `mcpServers` object means the task did not use MCP and is different from
missing MCP evidence. Credentials remain subject to the run-level secret audit
and must never be filled by compatibility data.

New local leaves preserve the redacted effective Kimi configuration at
`agent/.kimi/config.json.bak`. It records `max_retries_per_step=10` and
`max_steps_per_run=500` without retaining the runtime API key. Harbor's leaf
`config.json` records the five effective timeout multipliers.

For locally recorded schema-v2 evidence, `identity_source=local_runner` is
mandatory. The runner-generated gateway-shaped values are stable associations
for task, round, and recovery tracking; they are not claimed to have been issued
by the upstream gateway. The proxy metadata also records `target_url`,
`target_scheme`, `target_host`, and `target_path`. Every clean non-502 response
must have `complete: true`, a boolean `client_disconnected`, and an arrival
journal. Historical supplied artifacts without `recording_schema_version`
remain valid under the older observed contract.

`artifact-validate` treats `llmproxy/openai/meta.json` as the identity source
of truth. Its job, attempt, container, route, and derived session identifiers
must be nonempty and mutually consistent, and must agree with the identity
represented in `trial-record.json`. The MCP file must parse as JSON and contain
an object `mcpServers` field; a configured server is preserved verbatim as
observed evidence rather than normalized into a static expected value.

The runner can opt into the observed command shape with a contract profile:

```json
{
  "execution_profile": {
    "kind": "official_observed",
    "concurrency": 1
  }
}
```

This profile uses `kimi --print --output-format=stream-json --thinking
--command=...` and writes `agent/kimi-stream.jsonl` plus
`agent/execution-meta.json`.

The record keeps five independent dimensions so a zero reward is not confused
with an invalid execution:

| Dimension | Examples |
|---|---|
| identity | trial id, task name/checksum, job id, start/end timestamps |
| agent/environment | model, import path, timeout config, image/compose/MCP evidence |
| execution | phases, model steps, tool calls, reasoning size, tool errors |
| requests | request id, completion id, response metadata, `[DONE]`, finish reason, retry/partial sequence |
| outcome | reward, verifier error, execution validity, failure stage and termination reason |

The minimum termination taxonomy is:

`model_stream_incomplete`, `agent_timeout`, `agent_process_error`,
`mcp_tool_error`, `environment_build_error`, `verifier_failure`,
`agent_process_error_after_verifier`, `clean_reward_zero`, and
`clean_reward_one`.

Useful checks after a run are:

```bash
./runner/v2/kbench artifact-validate <trial-dir>
./runner/v2/kbench artifact-diff <official-trial-dir> <local-trial-dir> --json
```

`artifact-validate` enforces the stable file set for official-observed trials;
it deliberately leaves dynamic request counts, task-specific MCP contents, and
clean reward outcomes as observations rather than treating them as missing
files.

The run-level metrics also preserve reward-independent diagnostics such as
execution-invalid trials, incomplete model streams, timeout counts, MCP and
environment errors, verifier failures, and clean reward-zero trials.

At the run level, `runtime-versions.json` proves the actual Kimi CLI, Harbor,
Harbor Python, uv, and Terminal Bench 2 pins. It also records the versioned
cache root, hit/miss decisions, validated cache paths, and Kimi wheel SHA-256.
Cache reuse does not share run evidence or mutable task state: each run owns a
separate Terminal Bench worktree and jobs tree. The run-root
`release-manifest.json` states the
compatibility claim, while `finalization-manifest.json` binds that release to
one contract hash, complete leaf validation, secret/status gates, formal
round-mean metrics, and E2E/ITL diagnostics.

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

## SSE ITL Diagnostics

For each recorded response stream, calculate ITL from adjacent
`sla_metrics.ts_us` values. Report both the raw gap distribution and the share
of threshold-crossing gaps in the final 20% of timestamped chunks. A large gap
at a reasoning-to-tool-call boundary is a model/request-turn stall, not proof
that the whole task conversation was prematurely terminated; correlate it with
the next chunk, `finish_reason`, `[DONE]`, and the next request timestamp.
For runs with arrival journals, repeat the calculation using proxy wall-clock
event arrival times and report the service-timestamp minus proxy-arrival delta.
The derived report should include request-to-first-event, request-to-last-event,
last-event-to-next-request, task execution E2E, and the sum/share of threshold
crossing gaps. These values are diagnostic timing evidence and do not change
reward or official score accounting.
Do not infer real-time socket gaps from body file mtime or archive extraction
time. Older artifacts without arrival journals remain valid payload artifacts
but are not suitable for proxy-side inter-arrival analysis.

Credential-bearing keys and bearer/API-key values are replaced with
`<redacted>` before the files are written. Raw proxy request/response bodies
remain in the archive only when the upstream capture provides them; the
derived contract stores their sizes and integrity markers, not the body text.

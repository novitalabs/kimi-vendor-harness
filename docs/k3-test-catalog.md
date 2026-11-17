# K3 Test Catalog

This catalog defines the two execution classes for the K3 vendor matrix. The
executable source remains `models/kimi-k3/profile.yaml` and
`models/kimi-k3/suite-manifest.yaml`; this document explains the contract and
operator intent without creating a second matrix.

## Classification

| Class | Rows | Purpose | Result shape |
| --- | --- | --- | --- |
| `feature` | `1.0`-`6`, `18` | Fast feedback on API, streaming, multimodal, tool-call, parameter, and token contracts | Exact case/count/artifact completeness; no benchmark average |
| `precision` | `7`-`15`, `17` | Required long-running quality measurement across large datasets or coding tasks | Complete denominator plus score and profile-owned threshold/observation comparison |
| `optional_precision` | `16`, `K3-CB` | Optional BEAM and extended Kimi Code Bench evidence selected only when the operator wants the additional gate | Complete result is reported separately and does not gate the default required acceptance plan |

Every K3 suite declares `test_class` in the manifest. A suite must belong to
exactly one class. BEAM and K3-CB remain explicitly selectable optional suites;
they are not included in the default required acceptance modes.

## Feature Tests

| Row | Suite | Contract |
| --- | --- | --- |
| `1.0` | `stream-spec-p0` | Run the authoritative Moonshot streaming self-validation file at P0; uncovered items default to passed. |
| `1.1` | `stream-spec-p1` | Same file and artifact contract at P1. |
| `1.2` | `stream-spec-p2` | Same file and artifact contract at P2. |
| `2` | `image-test` | Replay the locked image fixture, including the 1000-image request; all 25 cases and response artifacts must be accounted for. |
| `3` | `kvv-params` | Run the official KVV parameter suite for 10 independent rounds; each round must meet the committed count. |
| `4` | `walle` | Run the official tool-call JSON-schema suite for 10 rounds with runner-owned retries and complete case artifacts. |
| `5` | `kvv-k3-features` | Run the official K3 feature suite for 10 rounds, preserving its official skip and retry behavior. |
| `6` | `kvv-prompt-tokens` | Run the official prompt-token suite for 10 independent rounds and preserve exact counts. |
| `18` | `toolcall-arguments-recovery` | Replay the two locked malformed/truncated histories; three streaming attempts per case must produce complete, non-empty valid JSON arguments. |

Feature rules:

- Keep inputs locked, bounded, and inspectable. A feature result passes only
  when the expected cases, skips, rounds, and required artifacts are complete.
- Separate `validity` from `status`: endpoint/auth/quota and transport failures
  are not model failures or score zeroes.
- Preserve suite-owned retries exactly. Profile-owned batch retries may only
  rerun eligible failed or invalid cases after the formal wave.
- Use explicit selectors for development diagnosis, for example
  `nvst develop --model kimi-k3 --select 1.0` or `--select 18`.

## Precision Tests

| Row | Suite | Contract |
| --- | --- | --- |
| `7` | `toolcall-thinking-low` | Artifact-reconstructed 113-sample tool-call benchmark at low thinking effort; complete `.eval` evidence and compare against the lower-bound official observations. |
| `8` | `toolcall-thinking-high` | Same benchmark at high thinking effort. |
| `9` | `toolcall-thinking-max` | Same benchmark at max thinking effort. |
| `10` | `inspect-ocr-low` | KVV OCRBench at low effort with the committed trial count and lower-bound tolerance. |
| `11` | `inspect-ocr-high` | KVV OCRBench at high effort. |
| `12` | `inspect-ocr-max` | KVV OCRBench at max effort. |
| `13` | `inspect-mmmu-low` | KVV MMMU Pro at low effort with the committed trial count and lower-bound tolerance. |
| `14` | `inspect-mmmu-high` | KVV MMMU Pro at high effort. |
| `15` | `inspect-mmmu-max` | KVV MMMU Pro at max effort. |
| `16` | `beam-1m` | Optional: generate the 1M BEAM answers, retry invalid task outputs under the runner contract, then judge and finalize without further vendor traffic. |
| `17` | `deepswe` | Run the pinned local DeepSWE runtime over 113 tasks; complete the required rounds, checkpoints, task locks, and preflight evidence. |
| `K3-CB` | `kimi-code-bench` | Optional: run the extended 89-task Kimi Code Bench set with the committed runner and recovery policy. |

Precision rules:

- Precision runs are never treated as a quick smoke test. They must retain
  dataset, runner, effort, concurrency, retry, and score provenance.
- A score is comparable only after the declared denominator and required
  artifacts are complete. Execution-invalid requests do not become model zeroes.
- Inspect rows use the profile's one-sided lower-bound tolerance; a higher
  score does not fail. BEAM and DeepSWE use their own profile-owned result
  contracts.
- Code Bench publishes one best-observed result for the selected run/session;
  comparison artifacts are not current acceptance evidence.

## Acceptance Usage

The existing `simple` and `full` modes preserve their production sequencing.
`--acceptance precision-core` is the scoped required precision run: Rows 7..9
(toolcall thinking effort) plus DeepSWE (Row 17, three rounds). BEAM (Row 16)
and K3-CB are optional explicit add-ons. It is not a full acceptance; unselected
rows stay `not_run` and the verdict only covers the selected required rows.

`--acceptance k3-sop` is the Moonshot pre-launch gate from the pinned K3 上线
SOP (revision 9). Its executable order is:

1. Rows `7`-`9`: tool-call thinking effort.
2. Rows `10`-`12`: OCRBench.
3. Rows `13`-`15`: MMMU Pro.
4. Row `5`: K3 new features.
5. Row `6`: prompt tokens.
6. Row `3`: Params.
7. Row `4`: tool-call JSON schema.
8. Rows `1.0` and `1.1`: Streaming P0 and P1.
9. Vendor-owned coverage: Row `2` Image, Row `17` DeepSWE,
   Row `18` tool-call arguments recovery. Row `16` BEAM and extended Kimi Code
   Bench are optional explicit suites.

Streaming P2 (`1.2`) is intentionally not part of `k3-sop`; it remains in the
full profile acceptance. The SOP run includes all long-test coverage and three
DeepSWE rounds, so it is an online pre-launch gate rather than a quick smoke
test. Passing it does not perform rollout or replace the required 10% -> 30% ->
100% observation and rollback process.

For targeted work, use `test_groups.feature`, `test_groups.precision`, and
`test_groups.optional_precision` as the selection inventory, then pass explicit
selectors to `develop`. Full acceptance runs required precision rows before the
feature group; optional suites are not implicitly added. A failed required suite
stops the acceptance plan and records remaining required suites as blocked.

The run-level delivery artifact is `test-artifacts/`. Use its `bundle.json` for
the overall verdict and completeness summary, then inspect the numbered suite
directory for the parameters, normalized result, and task/case/evidence index.
Each `suite-manifest.json` additionally records the effective `parameters`
index and the expected/observed `dependencies` (runner, repository commits, and
external runtime versions such as Harbor/Terminal Bench 2/Kimi CLI/uv for Code
Bench and Pier/DeepSWE/Kimi Code for row 17).
The bundle keeps required rows and optional BEAM/Code Bench results separate;
optional suites never change the required row count or default acceptance verdict.

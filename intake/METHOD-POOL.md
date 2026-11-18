# Method pool: test methods in this repository that no model manifest runs

When an official intake item has `coverage: gap`, look here first. Each entry is a runnable method
that exists but is not wired into any model (`suites/k3-family.yaml` + `models/*/suite-manifest.yaml`). Wiring one in changes an
acceptance matrix, so it is a reviewed change, never automatic.

Regenerate the "used vs unused" split by resolving every manifest suite's `paths`, `fixture`,
`command` and selector arguments against that suite's `cwd` (see the 2026-11-18 audit in git history);
a plain grep is wrong because manifest paths are cwd-relative.

## Official Moonshot methods (vendor/kvv-k3, read-only)

| Method | What it checks | Notes |
|---|---|---|
| `vendor/kvv-k3/tests/toolcall_reject_cases/test_toolcall_id.py` | Malformed tool-call requests (e.g. history tool call without `function.name`) are rejected with HTTP 400. Cases: `testdata/toolcall_reject_cases/rejected_case.jsonl`. | Listed in the kvv-k3 README as an official verifier (CI job `verify_toolcall_reject`), but absent from the K3 doc-444 row matrix. First candidate to raise when a new official document arrives. Sends `thinking` in Kimi style only. |
| `vendor/kvv-k3/verify_params_k3.py` | K3 parameter constraints, standalone CLI. | Overlaps row 3 (`tests/params`). |
| `vendor/kvv-k3/verify_tool_call_json_schema.py` | Walle MFJS schema cases, standalone CLI. | Overlaps row 4 (`tests/tool_call_json_schema`). |
| `vendor/kvv-k3/retry_with_kimi.py` | Retry wrapper for Inspect evals. | Tooling, not a check. |

## Novita methods (suites/novita-api)

Before writing an endpoint test for an output-parser defect, use the framework-neutral
`suites/novita-api/tests/parse_contract_vectors.jsonl` contract. It is the preferred method for
cases whose input is generated output rather than an HTTP request; each framework supplies an adapter.


| Method | What it checks | Runs when |
|---|---|---|
| `tests/toolcall_contract/test_toolcall_contract.py` | Engine-level tool-call invariants the official suites miss: invalid schema → 4xx, prompt-token stability, literal marker text, empty turns, undeclared tools, `response_format` with tools. Validated by fault injection (`tests/unit/test_toolcall_contract_suite.py`). | Any endpoint; see README "Tool-call contract suite". |
| `tests/interface/test_k3_contract.py` | Thinking modes and `max_tokens` override; preserve-thinking contract (currently skipped). | `--base-url/--api-key/--smoke-model` set. |
| `tests/interface/test_image_limit.py` | Endpoint accepts a request with 1000 image parts. | `K3_RUN_IMAGE_LIMIT=1`. Row 2's fixture already contains one 1000-image request. |
| `tests/interface/test_observability.py` | Provider deploy-id header and trace correlation; metrics snapshot shape. | Live endpoint; snapshot test needs `KIMI_METRICS_SNAPSHOT`. |
| `tests/interface/test_rate_limit.py` | Above-agreement TPM returns 429. | `K3_RUN_RATE_LIMIT=1` plus an explicit traffic budget; generates load on purpose. |
| `tests/streaming/test_stream_lifecycle_repro.py` | Stream lifecycle reproduction (unittest). | Diagnostic only. |
| `validations/run_vendor_img_test.py`, `validations/eval_with_extra_headers.py` | Older standalone runners for image cases and Inspect evals with extra headers. | Superseded by the row 2 replay adapter and the inspect adapter. |

`tests/unit/` under `suites/novita-api` are unit tests of the replay tooling, not endpoint checks.

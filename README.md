# kimi-vendor-harness

Test any inference engine's Kimi K3-family deployment (K3, K3-Flash, K3.1)
against Moonshot's vendor acceptance requirements. Give it an OpenAI-compatible
endpoint; it runs the official KVV suites plus Novita's own checks and produces
one verdict with evidence.

## Setup (once)

```bash
git clone git@github.com:wz1qqx/kimi-vendor-harness.git
cd kimi-vendor-harness
uv sync                                        # harness
(cd vendor/kvv-k3 && uv sync)                  # K3-family official verifier (K3, K3-Flash, K3.1)
```

Provide the endpoint token: `export KIMI_API_KEY=...`, or register it once in
the private registry (see [docs/credentials.md](docs/credentials.md)).

## Test an endpoint

`scripts/kvh` builds the secret-free access file and runs `nvst` locally:

```bash
URL=https://your-endpoint/v1
M=your-served-model-name

# Zero-traffic environment check
scripts/kvh --base-url $URL --served-model $M -- doctor --model kimi-k3

# Tool-call fast gate (run after every code change)
scripts/kvh --base-url $URL --served-model $M -- \
  develop --model kimi-k3 --select 6 --select 5 --select 4 --select 18

# Streaming spec + sampling params
scripts/kvh --base-url $URL --served-model $M -- \
  develop --model kimi-k3 --select 1.0 --select 1.1 --select 3

# Tool-call quality benchmark, effort low/high/max (113 tasks each)
scripts/kvh --base-url $URL --served-model $M -- \
  develop --model kimi-k3 --select 7 --select 8 --select 9

# Formal acceptance (long; includes row 17 DeepSWE, which needs a prepared host)
scripts/kvh --base-url $URL --served-model $M -- accept --model kimi-k3 --acceptance full
```

Options before `--`: `--think-mode opensource|kimi` (default `opensource`),
`--headers-json '{"X-Route":"pool-a"}'` for non-secret routing headers.
Other nvst commands pass through too, e.g.
`scripts/kvh --base-url $URL --served-model $M -- resume --run runs/kimi-k3/<run-id>`.
Use `--model kimi-k3` for K3 and K3.1; `kimi-k3-flash` for K3-Flash.

Rows 1–15 and 18 only need the endpoint. On a machine without extra setup, `doctor`
reports DeepSWE (row 17), BEAM (row 16) and Code Bench as blocked:

| Suite | Needs | Host setting (environment variable) |
|---|---|---|
| DeepSWE, row 17 | Pier and the deep-swe task corpus ([docs/deepswe.md](docs/deepswe.md)) | `NVST_DEEPSWE_ROOT`, `NVST_PIER_BIN` |
| BEAM, row 16 | Judge access ([docs/credentials.md](docs/credentials.md)) | `--judge-access` file |
| Code Bench | A runner host with Docker | `NVST_CODE_BENCH_HOST`, `NVST_CODE_BENCH_WORKDIR_ROOT`, `NVST_CODE_BENCH_CACHE_ROOT`, `NVST_CODE_BENCH_CONTAINER_PROXY` |

Host settings say where a suite runs, not what it tests, so they are not part of the shared test-set
definitions or the suite input hash. If your network needs a proxy to reach the endpoint or to download
benchmark datasets, set `HTTP(S)_PROXY` in your own shell; suites inherit it.

Results land in `runs/<model>/<run-id>/`: `acceptance.md` / `acceptance.json`
for the verdict, per-suite directories for evidence, and `test-artifacts/` as a
shareable bundle. `uv run nvst list --model kimi-k3` shows every row and selector.

## What each row checks

| Row | Suite | Checks | If it fails, look at |
|---|---|---|---|
| 1.0–1.2 | Streaming spec P0/P1/P2 | chunk shape, tool_call deltas and ids, usage, finish_reason | response assembly, streaming parser |
| 2 | Image | multimodal requests | vision input path |
| 3 | KVV `tests/params` | sampling params take effect | request handling |
| 4 | Walle `tests/tool_call_json_schema` | tool arguments satisfy the JSON schema | constrained decoding, tool parser |
| 5 | KVV `tests/k3_features` | tokenization, tool_choice, dynamic tools, response_format, thinking effort | prompt rendering, constrained decoding |
| 6 | KVV `tests/prompt_tokens` | prompt token counts match official | prompt rendering |
| 7–9 | Toolcall thinking benchmark | end-to-end tool-call quality vs official lower bound | everything above first |
| 10–15 | OCRBench / MMMU Pro | vision accuracy | — |
| 16 | BEAM (optional) | long-context memory; needs judge access | — |
| 17 | DeepSWE | agentic coding; needs a prepared Pier/Docker host | — |
| 18 | Toolcall arguments recovery | non-empty valid JSON arguments after long histories | tool parser, grammar |

Reading results: HTTP 429, timeouts and overload are `execution_invalid`
(rerun, don't fix code). Rows 7–15 fail only when more than 2pp below the
lowest official observation. Deterministic rows must pass 100%.

## Framework-neutral parser vectors

`suites/novita-api/tests/parse_contract_vectors.jsonl` is a parser contract derived from the
`k3-flash-kvv` behaviour. It covers typed arguments, raw JSON byte preservation, marker text inside
JSON strings, malformed-call isolation, undeclared-tool dropping, truncation, and attribute unescaping.
It is not an endpoint test: a framework adapter receives token-decoder output as chunks and returns
normalized reasoning/content/tool calls.

```bash
uv run python scripts/validate_parse_vectors.py
uv run python scripts/run_parse_vectors.py --adapter 'python /path/to/framework_parser_adapter.py'
```

The adapter protocol and expected normalized result are documented in
`scripts/run_parse_vectors.py`. Endpoint tests remain in `suites/novita-api/tests/toolcall_contract/`.

## Layout

```text
scripts/kvh                  local entrypoint (endpoint → nvst)
src/novita_self_test/        nvst CLI, orchestration, adapters, reporting
models/<model>/              profile, pinned requirement snapshot, rows + model-specific values
suites/k3-family.yaml        test sets shared by every K3-family model (K3, K3-Flash, K3.1)
suites/novita-api/           Novita endpoint tests and fixtures
suites/kimi-code-bench/      Kimi Code Bench runner
suites/deepswe/              DeepSWE Pier adapter
vendor/kvv-k3/               read-only snapshot of MoonshotAI/kvv-k3 (see VENDORED.md)
docs/                        contracts and per-model catalogs
```

Further reading: [docs/reference.md](docs/reference.md) (full operator
reference), [docs/k3-test-catalog.md](docs/k3-test-catalog.md),
[docs/k3-row-contracts.md](docs/k3-row-contracts.md),
[docs/contracts.md](docs/contracts.md), [docs/deepswe.md](docs/deepswe.md).

## Development

```bash
uv run pytest -q tests     # unit + e2e (no endpoint traffic)
```

## Tool-call contract suite (standalone)

`suites/novita-api/tests/toolcall_contract/` checks engine-level tool-call behaviour that the
official KVV suites do not cover (invalid-schema handling, prompt-token stability, literal
marker text, empty turns, undeclared tools, response_format with tools). It is not part of
the official row matrix; run it directly against an endpoint:

```bash
cd vendor/kvv-k3 && PYTHONPATH=../../suites/novita-api:. KVV_ROOT=. TC_SAMPLES=8 \
  TC_OBSERVATIONS_DIR=/tmp/tc .venv/bin/python -m pytest \
  ../../suites/novita-api/tests/toolcall_contract \
  --base-url $URL --smoke-model $M --think-mode opensource -q
```

Gate tests fail on any violation; observation tests write `toolcall-contract-observations.jsonl`
and never fail. The harness's own tests (`tests/unit/test_toolcall_contract_suite.py`) run the
suite against `tests/support/fake_k3_server.py` with one injected fault per known defect.

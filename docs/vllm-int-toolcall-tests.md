# Porting vllm-int K3 tool-call unit tests into this repository

Inventory of the K3 tool-call tests in `novitalabs/vllm-int`, branches `k3_production` (`9e2bdb7`) and
`k3-flash-kvv`, and which of them can become checks against an arbitrary framework.

## What exists

| Area | Location | k3_production | k3-flash-kvv |
|---|---|---|---|
| XTML output parser | `rust/src/parser/src/unified/kimi_k3.rs` | 36 | 43 |
| Grammar / structural tag | `rust/src/parser/src/unified/kimi_k3/structural_tag.rs` | 13 | 21 |
| response_format + tools grammar | `rust/src/chat/src/output/default/structural_tag.rs` | 10 | 16 |
| Stop / token accounting in output | `rust/src/chat/src/output/default/kimi_k3.rs` | 9 | file removed |
| Prompt renderer (5 golden fixtures) | `rust/src/chat/src/renderer/kimi_k3/tests.rs` | 20 | 22 |
| Request parsing / validation | `rust/src/chat/src/request.rs` | 15 | 15 |
| HTTP routes with an injected engine | `rust/src/server/src/routes/tests.rs` (`k3_http_*` etc., about 10 K3-related) | 134 total | 132 total |
| Python tool parser + request adjust | `tests/tool_use/test_kimi_k3_tool_parser.py` | 25 | 25 |
| Python reasoning parser | `tests/reasoning/test_kimi_k3_reasoning_parser.py` | 22 | 22 |
| Python renderer | `tests/renderers/test_kimi_k3.py` | 16 | 16 (modified) |
| Structural-tag registry, named tool_choice | `tests/tool_parsers/` | 26 | 27 |
| xgrammar backend, reasoning end | `tests/v1/structured_output/` | none | 2 new files |

The two branches disagree in places, for example `kimi_k3_malformed_call_attributes_fail_fast` (k3_production)
versus `..._drop_only_that_call` (k3-flash-kvv), and `rejects_removed_medium_thinking_effort` versus
`renders_medium_thinking_effort` (K3.1). A ported case must say which behaviour is the contract.

## What can become a check against a deployed endpoint

The deciding question is what the test feeds in. A deployed model decides its own output, so only tests
whose input is the request can run against an endpoint.

| Kind | Input | Endpoint-checkable? | Plan |
|---|---|---|---|
| Request validation | HTTP request | Yes, deterministic (status code) | Port as endpoint cases; most are already in KVV `k3_features`. |
| Prompt rendering | HTTP request | Yes, via `usage.prompt_tokens` | Port as prompt-token cases. Expected counts must come from the official `encoding_k3.py`, not from vllm-int. KVV row 6 already covers the parity cases (whitespace reasoning, non-object arguments, tool result matching). |
| Response rules that hold for any output | HTTP request | Yes (no XTML in content, `tool_choice=none` suppresses calls, id continuation, usage fields) | Partly in stream spec and `toolcall_contract`; add the rest there. |
| Output parsing | Model output tokens | No: an endpoint cannot be made to emit a chosen token sequence | Keep as framework-neutral parse vectors (output tokens → expected API result) run through a small per-framework adapter; endpoint suites only approximate them statistically. |
| Grammar construction | Request → grammar | Only statistically (`required` always calls, `strict` conforms) | Covered by Walle, `k3_features`, `toolcall_contract`; do not port grammar internals. |

The `k3_http_*` route tests look end-to-end but inject the engine's output tokens; they belong to the
"output parsing" row.

Most of the production incidents (truncated markers leaking into content, malformed call headers, undeclared
tools, marker text inside JSON arguments) are output-parsing cases, so the parse vectors are where the value is.

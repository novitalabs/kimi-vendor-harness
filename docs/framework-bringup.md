# Bring up Kimi on a new inference framework

Implement a **verified target contract**, verify each boundary at the right
layer, and retain a repair/regression trail. This is not vendor acceptance or
a quality score. Use the [README](../README.md) for installation and the
[agent protocol](agent-use.md) for authorization, freeze/execute and stop rules.

## 1. Establish the model contract before running endpoint gates

`profiles/kimi-k31-pro.yaml` is a **provisional, blocked scaffold**. It does not
inherit K3/Flash feature support, special token ids, effort ranges or defaults.
`plan --model kimi-k31-pro` can expose gaps without traffic, but target-bound
execution is refused while selected claims or the protocol remain unknown.

Obtain and record (do not invent):

1. Exact checkpoint/model-type/config revision and hashes.
2. Checkpoint encoder and tokenizer artifact revision/hashes, including ordinary
   text encoding versus structural token identities and generation prefixes.
3. The target API and stream protocol version: thinking aliases/defaults/efforts,
   tool declaration/choice behavior, argument root types, finish reasons, usage,
   JSON Schema subset and error policy.
4. What is model format, external API contract, optional platform policy, a
   reference-framework choice, or historical/unverified guidance.

Profile schema v2 has evidence-backed feature/effort claims:

```yaml
features:
  tool_call.choice.required:
    expectation: required
    evidence: ["<pinned API/source section reviewed by the maintainer>"]
```

Expectations are `required`, `optional`, `unsupported`, or `unknown`. Missing
claims mean **unknown**. Known claims require nonempty evidence references;
those references must be reviewed by a maintainer—the schema cannot verify a
remote contract's truth. `authority` records checkpoint/encoder/tokenizer/API
references and selected stream policy. Current runtime implements only the
explicit `kimi-stream-244` policy; an unknown/new policy needs implementation
and negative controls, not a convenient relabeling. A provisional profile also
blocks execution. Promote to ready only after checking authority and scope.

These are **target obligations**, not current framework support. Do not remove
or downgrade an unimplemented required claim to make tests disappear. Removing
it makes the requirement unknown and blocks execution. An actual contract
change needs updated evidence and review. The K3/Flash API profiles deliberately
do not certify checkpoint token parity; their checkpoint fields remain null.
Unknown multimodal support has no dedicated endpoint fixture and is not a pass.

## 2. Verify components offline

### Renderer/tokenizer parity

Generate authoritative goldens with the pinned checkpoint encoder separately
from the candidate framework renderer. Do not use one implementation to produce
both sides. Do not automatically import or execute arbitrary remote code: use
an explicitly approved local encoder/adapter environment.

`renderer-vector.schema.json` defines one JSONL record:

```json
{
  "id": "tools-history-required-thinking-off",
  "request": {"messages": [{"role": "user", "content": "<chosen fixture>"}]},
  "authority": {
    "checkpoint_revision": "<pinned revision>",
    "encoder_sha256": "<64 lowercase hex characters>",
    "tokenizer_sha256": "<64 lowercase hex characters>",
    "fixture_source": "<source test/section and extraction method>"
  },
  "expected_token_ids": [0]
}
```

This is a shape example, **not a valid Kimi golden**; all token values and hashes
must come from the actual checkpoint. No K3.1 Pro goldens are bundled.

```bash
kimi-harness renderer validate --vectors "$GOLDENS"
kimi-harness renderer run --vectors "$GOLDENS" \
  --adapter 'python /approved/framework_renderer_adapter.py' --timeout 30
```

The adapter consumes one JSON request per line:
`{id, request, authority}` and returns exactly one `{id, token_ids: [int]}` line
in order. Expected tokens are not sent to the adapter. It exits 0 after stdin
EOF. The process-group timeout, output-read cap, UTF-8 checks, and result-count
checks are shared with parser adapters. The developer-selected executable is
not sandboxed. Results expose first-difference token index, bounded context and
actual/expected lengths. File validation proves only the file contract, not
provenance authenticity or renderer correctness.

`examples/renderer-adapter.py --function my_bridge:render` is a small neutral
bridge. Implement that approved function to invoke your framework, return
`list[int]`, and keep logs on stderr. It is not a Kimi renderer or an oracle.

Suggested golden coverage: no tools/global/dynamic tools; none/auto/required and
named choice only if supported; thinking on/off/all verified efforts; whitespace
reasoning history; raw/non-object/malformed historical arguments; tool results
in reversed completion order and sparse ids; ordinary literal markers;
multimodal placeholders with pinned local bytes when applicable. Compare full
token sequences, not token totals. No automatic +3/+image corrections.

### Output parser

```bash
kimi-harness parser validate
kimi-harness parser run --adapter 'python /approved/parser_adapter.py' --timeout 30
```

The seven committed text vectors are a selected reference-derived contract,
not a K3.1 Pro checkpoint specification. Each declares `comparison: json` for
JSON value equivalence (key/whitespace ordering and equivalent numbers ignored,
bool distinct from number), or `bytes` where raw argument preservation is the
point. Channels and call names/order/index still compare exactly. Literal text
vectors cannot distinguish true special token ids from ordinary marker text;
use token-aware component evidence before claiming identity correctness.

Adapter stdin is JSONL, one independent parser run per line:

```json
{"id":"synthetic-demo","text":"<input>","chunks":["<in","put>"],"tools":["get_weather"],"finish":"stop"}
```

Reset parser state for each request, feed `chunks` in order, and finish using
`finish` (`stop` or `length`). Return one line, in request order:

```json
{"reasoning":"","content":"<parsed public text>","tool_calls":[{"name":"get_weather","index":0,"arguments":"{\"city\":\"Beijing\"}"}]}
```

`arguments` is always a string, including JSON-value comparisons. Index/order
and channels compare exactly. Exit 0 after EOF; keep logs on stderr. No expected
output is sent to the adapter. The neutral
[parser bridge](../examples/parser-adapter.py) accepts an explicitly approved
`--function module:parse`; implement it against your real framework, not the
vector's expected outputs. It is not a bundled Kimi parser or a sandbox.

## 3. Run phased endpoint checks

All selections below are zero-traffic plans. Inspect roles, applicability,
request count, output ceiling, opt-in requirements, and then freeze a target-bound
plan. Obtain explicit authorization before `run --execute`.

```bash
kimi-harness plan --model kimi-k3 --select smoke --format summary
kimi-harness plan --model kimi-k3 --select role:gate --format summary
kimi-harness plan --model kimi-k3 --select stage:conversation --format summary
kimi-harness plan --model kimi-k3 --select stage:regression --format summary
```

Use the correct verified model profile, not K3 as a shortcut for K3.1 Pro.
Selectors are unions: `--select role:gate --select stage:conversation` does not
mean their intersection. Use exact ids to freeze a bounded scope; see the
[taxonomy](capability-taxonomy.md) for metadata. Use `--filter` for AND refinement
and `--exclude` to subtract; use `--format catalog` or summary for file/line and
component/knowledge links. Run scoped `lint --file/--id` while authoring and full
lint before planning. Components are hints, not diagnoses.

Roles describe test purpose separately from requirement obligations:

- `gate`: externally observable format/contract invariant;
- `diagnostic`: prompt/model-dependent behavior, not a deterministic root cause;
- `observation`: explicit data collection without a capability pass claim;
- `platform`: selected policy such as dynamic-tool role/name validation.

Observation role and observation determinism must agree. Role controls the
no-pass claim; metadata disagreement is an authoring error, not a model result.

Required gate rows feed `gate_summary`; unknown gate obligations remain gaps.
Known unsupported cases are not mandatory applicable gates. The summary covers
**only selected required gates**, never the entire model requirement inventory.
Read overall status and `gate_summary.result_counts` as well: invalid execution, diagnostic
failures and observations are not concealed by a green gate subset. No score or
vendor acceptance is produced.

Ordinary cases use canonical step-level thinking intent, materialized as
`reasoning_effort` for `opensource` or `thinking` for `kimi`. Raw conflict/default
probes are separate and are not rewritten. Canonical thinking=true requires
explicit effort; the planner and materializer validate it rather than guessing
max/default. Confirm the deployment supports its declared dialect. Fixed output caps remain bounded; length-based
inconclusive results are not automatic permission to increase them/retry.
Known protocol violations remain failed even when generation is incomplete.

## 4. Genuine response-bound multi-turn scenarios

`conversation_state.bound_tool_roundtrip` has two turns in each wire mode, so
its full cost is **4 requests**. First it asks for one declared call. Next it
copies the actual assistant content/reasoning/call id/name/arguments into history,
appends the fixed canned tool result, and requests a structured continuation.
No tool command, URL, code or model-generated arguments are executed.

The step's frozen `continue_from` rule declares an earlier step index, exact
bounded `call_count` (1–8), and unique named canned `tool_results`. Canned result
order is intentional and can test out-of-order completion. Multiple calls to
the same approved tool receive that tool's fixed result; this is not argument-
dependent tool execution. A missing/failed/malformed parent or unmappable call
skips the child without spending its request allowance. Independent cases can
continue; service/transport failures still stop the run.

Frozen plans pin the request **template**, binding rule hash, fixed results,
assertions and request/output budgets, not future stochastic call ids. Each
actual request is built from previously validated responses. Evidence records
`request_sent`, `template_request_sha256`, `binding_sha256` and the actual
`request_sha256`. A skipped child may have template evidence with
`request_sent: false`; never claim it was transmitted. Bound history is limited
to 512 messages / 32 MiB serialized request bytes. These are safety ceilings,
not an engine context-length oracle. The complete scenario is always the unit
of replanning so dependency closure is retained.

The roundtrip is a semantic diagnostic; it does not prove token-level tool
result ordering. Keep long historical fixtures as opt-in regressions, and
minimize confirmed incidents into smaller component/scenario cases.

## 5. Repair, create a new plan, and compare offline

A framework fix changes runtime/source metadata: regenerate a plan for that
new target and get approval under the remaining aggregate budget. Do not replay
stale target-bound plans or automatically retry invalid execution.

```bash
kimi-harness plan --model kimi-k3 \
  --failed-from "$BASELINE_REPORT" --repeat 1 \
  --service-access "$NEW_ACCESS" --max-requests 4 --timeout 60 \
  --out "$RETEST_PLAN"
# review/authorize, then doctor --plan and run --plan as usual
kimi-harness compare --baseline "$BASELINE_REPORT" --candidate "$CANDIDATE_REPORT" \
  --scope candidate
```

`--failed-from` selects full cases containing failed/inconclusive/not-run rows.
Service/execution-invalid reports are rejected as a retest source; resolve
that prerequisite explicitly instead. Unknown case ids and empty failed sets
are errors. Explicit `--repeat` is required: the baseline repeat is shown if
omitted, never silently inherited or reduced. The sealed plan records baseline
report hash/repeat/unit count and whether repeat changed. Review all scope and
budget changes; this is zero-traffic planning, not a retry request.

Reports contain a scenario/assertion fingerprint separate from frozen target
identity. It includes model/profile, cases/fixtures/oracles, thinking/token-field
configuration and harness producer. Target URL/runtime/source identity may
change through a new plan. Changed scenario specs, producer/profile, mode/step/
repeat coverage are `not_comparable`, not `fixed`. Compare preserves each unit's
transition before case aggregation: a mode/step regression cannot be hidden by
another mode's fix. A partially fixed case remains changed evidence, not fixed.
Default `--scope all` marks missing cases not comparable. Explicit
`--scope candidate` compares only retested case ids and lists all untouched
baseline cases in `not_retested`; it never accepts missing units within a case.
Comparison status is failed for a regression, incomplete for invalid/insufficient
execution or scope/spec mismatch, otherwise passed; unchanged failures remain
failures. This is comparison completion, **not** candidate acceptance. Evidence links and before/after statuses must be
read. Use trusted reports; schemas do not authenticate who generated them.

## Evidence and known gaps

HTTP evidence includes start/end/first-byte timing, received byte count and
byte hash. Strict UTF-8 errors retain offending offsets/hex; replacement text
is display-only and never a valid pass. This is bounded diagnostic evidence,
not profiler/throughput data. Request id headers are hints; inspect the actually
selected runtime and service logs under separately approved read-only access.

K3.1 Pro checkpoint-generated goldens/API verification, dedicated multimodal
endpoint cases, full effort/alias/schema matrices, named/parallel-tool contracts,
full usage details and distributed scheduler/grammar-preemption coverage remain
unverified or not implemented. Do not infer coverage from a knowledge page,
profile claim, case count or all-green subset. Add a minimized case and negative
control for each confirmed boundary and link evidence/source revisions.

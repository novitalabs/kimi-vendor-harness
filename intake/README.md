# Official test intake

Each external Moonshot document that lists the tests it will run for a model is
recorded here as one directory:

```text
intake/<model>/<document-revision>/items.yaml
```

`items.yaml` is the only input to the acceptance set. Every test item in the
document appears exactly once, whether or not Moonshot supplied a recipe for it.

## Item fields

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Stable id, unique within the file. Use the document's own row or item number when it has one. |
| `source` | yes | Where the item is stated: section, table, row. Copy the locator, not a paraphrase. |
| `requirement` | yes | The document's wording, verbatim. |
| `group` | yes | `feature` or `precision`. Copy the document's grouping; do not infer it. |
| `mandatory` | yes | `true` when the document makes the item part of acceptance. |
| `official_result` | no | The official number or pass count, as written, plus the `kind` used to compare it. |
| `recipe` | yes | `none`, or a path under `intake/<model>/<revision>/recipes/` that holds Moonshot's recipe. |
| `coverage` | yes | One of the states below. |
| `method` | when coverage is not `gap` | Path to the test file or runner, relative to the repository root. |
| `notes` | no | Anything a reviewer needs to know. |

## Coverage states

| State | Meaning |
|---|---|
| `wired` | The model manifest declares a suite for this row, and that suite's runtime requirements are met on the host where it runs. Declaration alone is not proof that the suite has run. |
| `entry_only` | A method file exists, but no manifest suite runs it, or its external runtime is not available on this host. |
| `gap` | No method yet. Writing one is a separate, reviewed change. |

`entry_only` exists because a path that resolves is not the same as a test that
runs. The K3 seed already has rows like that: row 2's `vendor-img-testcases-inhouse-3.jsonl`
does not resolve from the repository root, and row 17 names a runner by description
rather than by path.

## Filling gaps

For an item with `coverage: gap`, first check [METHOD-POOL.md](METHOD-POOL.md): runnable methods that
exist in the repository but no manifest runs. Only write a new method when none fits. Either way,
wiring a method into a model manifest is a reviewed change.

## Validation

```bash
uv run python scripts/validate_intake.py intake/kimi-k3/444
```

The validator checks the schema, unique ids, the coverage vocabulary, and that each
`method` path exists. It exits non-zero on any violation.

## Running against a developer's endpoint

Validate and audit first, then run the selected rows against the endpoint:

```bash
uv run python scripts/validate_intake.py intake/kimi-k3/444
uv run python scripts/audit_intake.py intake/kimi-k3/444

scripts/kvh --base-url https://<endpoint>/v1 --served-model <model> -- \
  develop --model kimi-k3 --select 6 --select 5 --select 4 --select 18
```

`--select` takes the row ids from `items.yaml`. Every intake row id has a matching
manifest selector, which was checked, but the full path has not been run end to end
against a live endpoint yet. A row marked `wired` has a declared suite; it has not
necessarily run on this host.

## Fake-endpoint run of row 6 (resolved)

`develop --select 6` against `tests/support/fake_k3_server.py` now ends `failed` (valid) with every
prompt-token case reported as a real mismatch, which is correct: the fake does not render K3 prompts.
Earlier runs ended `inconclusive` because of three harness bugs, all fixed with regression tests:

1. The fake crashed on list-shaped message content (vision cases), surfacing as `Connection error.`
2. Failure classification read pytest's whole `longrepr`, which includes the test's source; an
   `except (openai.APITimeoutError, ...)` clause there made every assertion failure look retryable.
   Only the `E ` exception lines are classified now.
3. The adapter passed `--artifact-dir <path>` as two tokens. pytest picks its rootdir before plugin
   options are known, so it treated the path as a test path and chose this repository as rootdir
   (and its `pyproject.toml` as config) instead of `vendor/kvv-k3`. Recorded node ids gained a
   `vendor/kvv-k3/` prefix, and each rerun wave, run from `vendor/kvv-k3`, collected zero tests.
   The option is now passed as `--artifact-dir=<path>`. This affected rows 3 and 6 for both
   kimi-k3 and kimi-k3-flash (and which pytest config every vendor/kvv-k3 suite loaded).

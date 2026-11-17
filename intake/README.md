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

## Validation

```bash
uv run python scripts/validate_intake.py intake/kimi-k3/444
```

The validator checks the schema, unique ids, the coverage vocabulary, and that each
`method` path exists. It exits non-zero on any violation.

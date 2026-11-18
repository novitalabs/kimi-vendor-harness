# Vendored snapshot: MoonshotAI/kvv-k3

- Upstream: https://github.com/MoonshotAI/kvv-k3 (private), branch `k3`
- Commit: `6ec0eaad1ca98933327cf571c0d8b78ab8476551` (also in `VENDORED_COMMIT`)
- Why vendored: the upstream repository is private and team members do not have
  access. This snapshot is read-only; do not edit files here.
- `docs/beam-1m-provenance.json` records the public BEAM source, converter, counts, and file hashes.
- Omitted: `toolcall_benchmark/toolcall_thinking_samples.jsonl` and
  `toolcall_benchmark/toolcall_non_thinking_samples.jsonl` (LFS objects that were
  never fetched locally). No harness suite reads them; rows 7–9 use
  `suites/novita-api/fixtures/toolcall-thinking-samples.v1.json.gz`.

Upstream's `.gitattributes` (Git LFS rules) is removed: data files are stored as
regular Git blobs here so team members do not need Git LFS.

To refresh once upstream access exists: replace this directory with a checkout
of the new commit (LFS objects pulled), update `VENDORED_COMMIT`, and update
`verifier_repository.commit` in `models/*/requirements/source.yaml`.

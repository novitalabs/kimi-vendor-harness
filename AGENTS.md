# kimi-agentic-harness

This repository is a long-lived Kimi agentic capability and regression harness.
It is not a vendor acceptance platform and does not run BEAM, MMMU Pro, OCRBench,
DeepSWE or model-quality score benchmarks.

- Organize cases by capability under `capabilities/`, not by official row.
- Keep each case once. Use tags, `likely_components`, `source`, `knowledge` and
  regression metadata for cross-cutting views.
- Treat checkpoint encoder/tokenizer/config as the authority for prompt/token
  behavior. KVV and reference implementations are versioned sources, not a
  universal runtime dependency or K3.1 contract.
- Keep official/KVV-derived cases small and adapted. Record original path,
  commit and adaptation in the case; never imply complete official coverage.
- `capabilities plan` and `doctor` are zero traffic. Endpoint runs require
  explicit `--execute`, a request ceiling and no implicit retries.
- Separate endpoint cases, parser adapter vectors and harness-local unit tests.
  Parser vectors never prove endpoint behavior.
- HTTP auth/quota/timeout/429/5xx/connection problems are service or execution
  evidence, never model failures. An expected invalid request must distinguish a
  4xx contract response from a server crash.
- Keep endpoint credentials outside Git. Access files contain only the API-key
  environment variable name and non-secret headers. Redact request/response
  artifacts before writing them.
- Results belong under ignored `NVST_RESULTS_ROOT` (or the default ignored
  `runs/`). Do not write a repository-local environment or test output.
- Before editing implementation, read applicable workspace coding guidance;
  make focused edits and run the smallest relevant offline tests.

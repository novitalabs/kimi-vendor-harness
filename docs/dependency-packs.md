# Dependency and runtime contract

The capability harness has one user-owned Python environment outside the
checkout. It does not install KVV, Inspect, DeepSWE, Pier, Docker images or
benchmark datasets.

```bash
uv venv "$HOME/.cache/kimi-agentic-harness/venv" --python 3.12 --allow-existing
UV_PROJECT_ENVIRONMENT="$HOME/.cache/kimi-agentic-harness/venv" uv sync --locked
export PATH="$HOME/.cache/kimi-agentic-harness/venv/bin:$PATH"
export NVST_HARNESS_PYTHON="$HOME/.cache/kimi-agentic-harness/venv/bin/python"
```

The installed test command is `kimi-harness`. Endpoint tests use `plan`,
`doctor`, and `run`; component checks use `parser validate/run` and
`renderer validate/run`; `compare` reads existing reports without traffic.
The parser conformance adapter is supplied by the framework developer and is
invoked explicitly. It is not an inference endpoint and has no shared venv
contract. Updating with `uv sync --locked` also updates the installed command
entry points; no separate scripts or compatibility aliases are required.

Endpoint credentials are supplied through an environment variable named by the
secret-free service-access file. Results are written to ignored `runs/` or
`NVST_RESULTS_ROOT`. No repository-local `.venv` or shared build host is part of
the workflow.

## Optional external material

A framework may provide a checkpoint tokenizer/encoder or a token-aware parser
adapter for offline conformance. Record its commit and command in the run
metadata. A KVV checkout may be used by a maintainer while extracting new cases,
but it is not an installed pack or a runtime prerequisite.

There is no CI or benchmark pack in this repository. Large benchmark systems
belong to their own evaluation projects; a minimized regression can be imported
with provenance and a request budget.

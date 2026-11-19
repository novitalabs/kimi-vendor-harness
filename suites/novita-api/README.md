# Novita API Suite

This subtree contains the Novita-owned endpoint tests, fixtures, replay helpers,
and BEAM fallback imported from the KVV fork with full history. K3-family
execution uses the read-only snapshot `../../vendor/kvv-k3`.

Run this suite through `nvst`; direct pytest is useful only for offline
collection or suite development:

```bash
export KVV_ROOT="$(cd ../../vendor/kvv-k3 && pwd)"
export PYTHONPATH="$PWD:$KVV_ROOT"
export NVST_KVV_PYTHON="${NVST_KVV_PYTHON:-$HOME/.cache/novita-kimi-vendor/venvs/kvv-k3/bin/python}"
"$NVST_KVV_PYTHON" -m pytest --collect-only -q tests
```

Do not add endpoint-specific preflight scripts, dynamic endpoints, or literal
credentials here. Runtime identity and the API-key environment-variable name
come from a secret-safe `service-access.json`.

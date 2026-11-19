# Per-user dependency packs

The repository is the shared test library. Execution environments belong to the
person running a test: each user chooses their own virtualenvs, Hugging Face
cache, endpoint credentials, Docker host, and long-test runtime.

No setup pack sends inference traffic. `setup-pack.sh` installs only local
Python dependencies or prints the external-runtime contract; `check-packs.py`
performs offline checks.

## Pack matrix

| Pack | Enables | Owned by the user | Setup |
|---|---|---|---|
| `core` | `nvst`, KVV rows 1–6 and 18, endpoint smoke/fast gates | user-owned harness and KVV virtualenvs | `scripts/setup-pack.sh core` |
| `vision` | OCRBench/MMMU rows 10–15 | KVV env, `HF_HOME`, datasets cache | `scripts/setup-pack.sh vision` |
| `deepswe` | DeepSWE row 17 | `NVST_DEEPSWE_ROOT`, `NVST_PIER_BIN`, Docker/task images | prepare externally, then `check-packs.py --pack deepswe` |

## New machine: core only

```bash
git clone git@github.com:wz1qqx/kimi-vendor-harness.git
cd kimi-vendor-harness
scripts/setup-pack.sh core
export NVST_HARNESS_PYTHON="$HOME/.cache/novita-kimi-vendor/venvs/harness/bin/python"
export NVST_KVV_PYTHON="$HOME/.cache/novita-kimi-vendor/venvs/kvv-k3/bin/python"
export HF_HOME="$HOME/.cache/novita-kimi-vendor/huggingface"
scripts/check-packs.py --pack core
# Optional: keep ignored run evidence outside the checkout.
export NVST_RESULTS_ROOT="$HOME/.cache/novita-kimi-vendor/runs"
```

The core pack never needs access to Moonshot's private repository: `vendor/kvv-k3`
is the pinned repository snapshot committed in this private harness. The two
virtualenvs are outside the checkout; `NVST_HARNESS_PYTHON` and `NVST_KVV_PYTHON`
tell the tools which user-owned interpreters to use. The setup script never
requires or modifies a repository-local `.venv/`.

Provide a token through `KIMI_API_KEY` or the local credential registry; see
[credentials.md](credentials.md). Then use `scripts/kvh` with a deliberate
selector. `doctor` is zero-vendor-traffic; it only checks the local contract and
credential environment.

## Vision pack

```bash
scripts/setup-pack.sh vision
export HF_HOME="$HOME/.cache/novita-kimi-vendor/huggingface"
scripts/check-packs.py --pack vision
```

Datasets are downloaded lazily by the user's own KVV environment. They are not
committed to this repository and are not shared between users.

## DeepSWE pack

The corpus, Pier installation, Docker images, and runtime are intentionally not
bundled. Install or provision them in a user-owned location, then set:

```bash
export NVST_DEEPSWE_ROOT="$HOME/runtimes/deepswe"
export NVST_PIER_BIN="$HOME/.local/bin/pier"
scripts/check-packs.py --pack deepswe
```

The K3 profile still pins the expected DeepSWE commit, Pier version, task count,
and score contract. The runtime path is only a host setting and is not part of
the shared test-set definition.

## Full local preflight (no endpoint traffic)

```bash
scripts/check-packs.py --pack all --model kimi-k3
"$NVST_BIN" list --model kimi-k3
"$NVST_HARNESS_PYTHON" -m pytest -q tests
```

A blocked optional pack is expected on a normal laptop. It becomes a blocker only
when the user selects the corresponding row or acceptance mode.

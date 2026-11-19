#!/usr/bin/env bash
# Install one optional dependency pack into user-owned paths.
# Usage: scripts/setup-pack.sh core | vision | deepswe | all
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pack="${1:-}"
case "$pack" in
  core|vision|deepswe|all) ;;
  *) echo "usage: $0 {core|vision|deepswe|all}" >&2; exit 2 ;;
esac

# Nothing is installed into the repository and no endpoint traffic is generated.
venv_root="${NVST_VENV_ROOT:-$HOME/.cache/novita-kimi-vendor/venvs}"
hf_home="${HF_HOME:-$HOME/.cache/novita-kimi-vendor/huggingface}"
harness_venv="${NVST_HARNESS_VENV:-$venv_root/harness}"
kvv_venv="${NVST_KVV_VENV:-$venv_root/kvv-k3}"
mkdir -p "$venv_root" "$hf_home"
export HF_HOME="$hf_home"

if [[ "$pack" == core || "$pack" == vision || "$pack" == all ]]; then
  uv venv "$harness_venv" --python 3.12 --allow-existing
  UV_PROJECT_ENVIRONMENT="$harness_venv" uv sync --locked --project "$root"
  uv venv "$kvv_venv" --python 3.12 --allow-existing
  UV_PROJECT_ENVIRONMENT="$kvv_venv" uv sync --locked --project "$root/vendor/kvv-k3"
  cat <<EOF
Core pack ready. Export these in each new shell:
  export NVST_HARNESS_PYTHON="$harness_venv/bin/python"
  export NVST_KVV_PYTHON="$kvv_venv/bin/python"
  export HF_HOME="$hf_home"
EOF
fi
if [[ "$pack" == vision || "$pack" == all ]]; then
  echo "vision pack ready; datasets download lazily under HF_HOME=$HF_HOME"
fi
if [[ "$pack" == deepswe || "$pack" == all ]]; then
  cat <<'EOF'
DeepSWE is an external runtime and is intentionally not downloaded implicitly.
Prepare your own pinned DeepSWE/Pier installation, then export:
  NVST_DEEPSWE_ROOT=/path/to/runtime-root
  NVST_PIER_BIN=/path/to/pier
The committed profile checks the DeepSWE commit, Pier version, and 113-task inventory.
EOF
fi
printf 'pack=%s ready\n' "$pack"

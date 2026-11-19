#!/usr/bin/env python3
"""Check per-user dependency packs without contacting a model endpoint.

Examples:
  scripts/check-packs.py --pack core
  scripts/check-packs.py --pack vision
  scripts/check-packs.py --pack deepswe
  scripts/check-packs.py --pack all --model kimi-k3
"""

from __future__ import annotations

import argparse
import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_imports(names: list[str], label: str, python: Path | None = None) -> list[str]:
    if python is not None:
        try:
            probe = subprocess.run(
                [str(python), "-c", "import " + ",".join(names)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            print(f"BLOCKED {label}: {exc}")
            return names
        if probe.returncode:
            print(f"BLOCKED {label}: {probe.stderr.strip() or 'import failed'}")
            return names
        print(f"READY   {label} ({python})")
        return []
    missing = []
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as exc:  # pragma: no cover - depends on user's environment
            missing.append(f"{name} ({type(exc).__name__})")
    if missing:
        print(f"BLOCKED {label}: missing {', '.join(missing)}")
    else:
        print(f"READY   {label}")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pack", choices=["core", "vision", "deepswe", "all"], default="core")
    parser.add_argument("--model", choices=["kimi-k3", "kimi-k3-flash"], default="kimi-k3")
    args = parser.parse_args()

    failures = 0
    if args.pack in {"core", "vision", "all"}:
        harness_python = Path(os.environ.get("NVST_HARNESS_PYTHON", str(Path.home() / ".cache/novita-kimi-vendor/venvs/harness/bin/python")))
        kvv_python = Path(os.environ.get("NVST_KVV_PYTHON", str(Path.home() / ".cache/novita-kimi-vendor/venvs/kvv-k3/bin/python")))
        if harness_python.is_file():
            print(f"READY   harness environment: {harness_python}")
            failures += len(check_imports(["httpx", "openai", "yaml", "pytest"], "core", harness_python))
        else:
            print("BLOCKED harness environment: run scripts/setup-pack.sh core")
            failures += 1
        if kvv_python.is_file():
            print(f"READY   KVV environment: {kvv_python}")
        else:
            print("BLOCKED KVV environment: run scripts/setup-pack.sh core")
            failures += 1
    if args.pack in {"vision", "all"}:
        kvv_python = Path(os.environ.get("NVST_KVV_PYTHON", str(Path.home() / ".cache/novita-kimi-vendor/venvs/kvv-k3/bin/python")))
        failures += len(check_imports(["inspect_ai", "datasets", "PIL"], "vision datasets", kvv_python))
        print(f"INFO    HF_HOME={os.environ.get('HF_HOME', str(Path.home() / '.cache/novita-kimi-vendor/huggingface'))}")
    if args.pack in {"deepswe", "all"}:
        root = os.environ.get("NVST_DEEPSWE_ROOT")
        pier = os.environ.get("NVST_PIER_BIN")
        if root and pier and Path(root).is_dir() and Path(pier).is_file():
            print(f"READY   DeepSWE runtime: {root}")
        else:
            print("BLOCKED DeepSWE: set NVST_DEEPSWE_ROOT and NVST_PIER_BIN to a user-owned installation")
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

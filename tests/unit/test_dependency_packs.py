from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_setup_pack_is_shell_valid_and_does_not_use_repo_venv() -> None:
    script = (ROOT / "scripts/setup-pack.sh").read_text(encoding="utf-8")
    assert "UV_PROJECT_ENVIRONMENT" in script
    assert "NVST_VENV_ROOT" in script
    assert "NVST_KVV_VENV" in script
    assert "NVST_HARNESS_VENV" in script
    assert "--locked" in script


def test_check_packs_reports_missing_optional_external_hosts(tmp_path: Path) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("NVST_")}
    env["NVST_HARNESS_PYTHON"] = str(tmp_path / "missing-harness-python")
    env["NVST_KVV_PYTHON"] = str(tmp_path / "missing-kvv-python")
    result = subprocess.run(
        [os.environ.get("PYTHON", "python3"), "scripts/check-packs.py", "--pack", "all"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "BLOCKED" in result.stdout
    assert "NVST_DEEPSWE_ROOT" in result.stdout

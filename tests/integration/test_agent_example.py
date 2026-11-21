"""Exercise the documented code-agent protocol without an external endpoint."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from novita_self_test.contracts import validate_contract

ROOT = Path(__file__).resolve().parents[2]


def test_documented_loopback_example_is_bounded_and_links_evidence(tmp_path):
    out = tmp_path / "agent-demo"
    process = subprocess.run([sys.executable, str(ROOT / "examples/agent-loopback.py"), "--out", str(out)],
                             cwd=tmp_path, text=True, capture_output=True, timeout=40)
    assert process.returncode == 0, process.stderr
    summary = json.loads(process.stdout)
    assert summary["status"] == "exercise_passed"
    assert summary["endpoint_verdict"] == "failed"
    assert summary["actual_loopback_requests"] == 2
    assert summary["detected_codes"] == ["tool_name_undeclared"]
    plan = json.loads((out / "plan.json").read_text())
    validate_contract(plan, "plan.schema.json")
    report = json.loads((out / "report.json").read_text())
    validate_contract(report, "report.schema.json")
    assert report["plan_sha256"] == plan["plan_sha256"]
    for file in summary["evidence"]:
        value = json.loads((Path(summary["run_dir"]) / file).read_text())
        validate_contract(value, "evidence.schema.json")
    for file in out.rglob("*.json"):
        assert "synthetic-loopback-only-key" not in file.read_text()

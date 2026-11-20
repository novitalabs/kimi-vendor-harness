from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "capabilities/prompt_rendering/k3-parser-conformance.jsonl"


def test_parser_vectors_validate_and_preserve_provenance() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from parser_conformance_lib import load_vectors

    cases = load_vectors(VECTORS)
    assert len(cases) == 7
    assert {case["source_revision"] for case in cases} == {
        "novitalabs/vllm-int:k3-flash-kvv@c94c52e24e40"
    }


def test_parser_validator_rejects_duplicate_id(tmp_path: Path) -> None:
    case = json.loads(VECTORS.read_text().splitlines()[0])
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(case) + "\n" + json.dumps(case) + "\n")
    process = subprocess.run(
        [sys.executable, "scripts/validate-parser-conformance.py", str(path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 1
    assert "duplicate id" in process.stderr

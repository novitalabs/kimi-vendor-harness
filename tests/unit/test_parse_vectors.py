from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "suites/novita-api/tests/parse_contract_vectors.jsonl"


def test_parse_vectors_validate_and_preserve_source_cases() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from validate_parse_vectors import load_vectors

    cases = load_vectors(VECTORS)
    assert len(cases) == 7
    assert {case["source_revision"] for case in cases} == {
        "novitalabs/vllm-int:k3-flash-kvv@c94c52e24e40"
    }
    assert {case["source_test"].split("::")[-1] for case in cases} == {
        "kimi_k3_parses_reasoning_response_and_typed_tool_call",
        "kimi_k3_json_block_arguments_pass_through_raw",
        "kimi_k3_marker_text_inside_json_string_is_argument_text",
        "kimi_k3_malformed_call_attributes_drop_only_that_call",
        "kimi_k3_undeclared_tool_call_is_dropped",
        "kimi_k3_finish_drops_truncated_response_close_marker",
        "kimi_k3_attribute_values_are_unescaped",
    }


def test_parse_vector_validator_rejects_duplicate_or_incomplete_case(tmp_path: Path) -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    from validate_parse_vectors import load_vectors

    case = json.loads(VECTORS.read_text().splitlines()[0])
    case["expected"].pop("content")
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(case) + "\n" + json.dumps(case) + "\n")
    with pytest.raises(ValueError, match="expected must contain|duplicate id"):
        load_vectors(path)


def test_parse_vector_runner_sends_chunks_and_reports_mismatch(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter.py"
    adapter.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "  req=json.loads(line)\n"
        "  print(json.dumps({'reasoning':'','content':'','tool_calls':[]}), flush=True)\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [sys.executable, "scripts/run_parse_vectors.py", "--adapter", f"{sys.executable} {adapter}",
         "--vectors", str(VECTORS)],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert process.returncode == 1
    assert "typed_arguments_and_channels" in process.stderr

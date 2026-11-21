from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
import shlex

from novita_self_test.parser_vectors import load_vectors

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "capabilities/prompt_rendering/k3-parser-conformance.jsonl"


def test_parser_vectors_validate_and_preserve_provenance() -> None:
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
        [sys.executable, "-m", "novita_self_test", "parser", "validate", "--vectors", str(path)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert process.returncode == 2
    assert json.loads(process.stdout)["problems"][0]["code"] == "parser_vectors_invalid"


def run_adapter(tmp_path, body, *options):
    adapter = tmp_path / "adapter.py"
    adapter.write_text(body, encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "novita_self_test", "parser", "run", "--adapter", shlex.join([sys.executable, str(adapter)]), *options],
        cwd=ROOT, text=True, capture_output=True, check=False, timeout=10,
    )


def test_parser_adapter_timeout_including_partial_line(tmp_path):
    process = run_adapter(tmp_path, "import sys,time\nsys.stdout.write('{'); sys.stdout.flush(); time.sleep(60)\n",
                          "--timeout", "0.15")
    assert process.returncode == 2
    assert json.loads(process.stdout)["problems"][0]["code"] == "adapter_timeout"


def test_parser_adapter_bad_json_wrong_count_and_nonzero_exit(tmp_path):
    process = run_adapter(tmp_path, "import sys\nfor line in sys.stdin: print('[]', flush=True)\nsys.exit(3)\n")
    assert process.returncode == 2
    output = json.loads(process.stdout)
    assert output["problems"][0]["code"] == "adapter_exit_nonzero"
    assert output["results"][0]["problems"][0]["code"] == "adapter_result_invalid"
    process = run_adapter(tmp_path, "import sys\nsys.stdin.read()\n")
    assert process.returncode == 1
    assert json.loads(process.stdout)["problems"][0]["code"] == "adapter_result_count_mismatch"


def test_parser_adapter_protocol_passes_and_extra_lines_fail(tmp_path):
    body = (
        "import json,sys\n"
        f"cases={{x['id']:x['expected'] for x in map(json.loads,open({str(VECTORS)!r}))}}\n"
        "for line in sys.stdin:\n"
        " req=json.loads(line); assert ''.join(req['chunks'])==req['text']; print(json.dumps(cases[req['id']]), flush=True)\n"
    )
    process = run_adapter(tmp_path, body)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)["vector_count"] == 7
    assert json.loads(process.stdout)["trial_count"] == 21
    process = run_adapter(tmp_path, body + "print('{}')\n")
    assert process.returncode == 1
    assert json.loads(process.stdout)["problems"][0]["code"] == "adapter_result_count_mismatch"


def test_parser_adapter_noisy_stderr_cannot_deadlock(tmp_path):
    body = (
        "import json,sys\n"
        "sys.stderr.write('synthetic diagnostics\\n' * 20000);sys.stderr.flush()\n"
        f"cases={{x['id']:x['expected'] for x in map(json.loads,open({str(VECTORS)!r}))}}\n"
        "for line in sys.stdin: print(json.dumps(cases[json.loads(line)['id']]), flush=True)\n"
    )
    process = run_adapter(tmp_path, body, "--timeout", "2")
    assert process.returncode == 0, process.stderr


def test_parser_adapter_timeout_reaps_child_process(tmp_path):
    import os
    import time
    import pytest
    pidfile = tmp_path / "child.pid"
    body = (
        "import subprocess,sys,time\n"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'])\n"
        f"open({str(pidfile)!r},'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    process = run_adapter(tmp_path, body, "--timeout", "0.5")
    assert process.returncode == 2
    pid = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("adapter child survived process-group timeout")


def test_parser_adapter_rejects_invalid_timeout_and_binary_results(tmp_path):
    process = run_adapter(tmp_path, "pass\n", "--timeout", "nan")
    assert process.returncode == 2
    process = run_adapter(tmp_path, "import sys\nsys.stdin.read();sys.stdout.buffer.write(bytes([255]))\n")
    assert process.returncode == 1
    assert json.loads(process.stdout)["problems"][0]["code"] == "adapter_encoding_invalid"

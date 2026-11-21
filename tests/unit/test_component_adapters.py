from __future__ import annotations

import copy
import json
from pathlib import Path
import shlex
import sys

from novita_self_test.renderer_conformance import run
from novita_self_test.parser_conformance import comparison_value, equivalent
from novita_self_test.config import canonical_hash


def golden():
    return {"id": "synthetic-only", "request": {"messages": [{"role": "user", "content": "test"}]},
            "authority": {"checkpoint_revision": "synthetic-not-a-model", "encoder_sha256": "0"*64,
                          "tokenizer_sha256": "1"*64, "fixture_source": "synthetic protocol self-test, not Kimi evidence"},
            "expected_token_ids": [1, 2, 3]}


def test_renderer_token_comparison_and_first_difference(tmp_path):
    vectors = tmp_path / "golden.jsonl"
    vectors.write_text(json.dumps(golden()) + "\n")
    assert run(vectors)["status"] == "passed"
    adapter = tmp_path / "adapter.py"
    adapter.write_text('import sys,json\nfor line in sys.stdin: print(json.dumps({"id":json.loads(line)["id"],"token_ids":[1,2,3]}))\n')
    command = shlex.join([sys.executable, str(adapter)])
    assert run(vectors, command, 2)["status"] == "passed"
    adapter.write_text(adapter.read_text().replace('[1,2,3]', '[1,9]'))
    result = run(vectors, command, 2)
    assert result["status"] == "failed"
    assert result["results"][0]["first_difference"]["index"] == 1
    assert result["results"][0]["first_difference"]["expected_context"] == [1, 2, 3]


def test_renderer_requires_provenance_and_handles_adapter_timeout(tmp_path):
    vectors = tmp_path / "golden.jsonl"
    value = golden()
    del value["authority"]["encoder_sha256"]
    vectors.write_text(json.dumps(value) + "\n")
    assert run(vectors)["status"] == "blocked"
    vectors.write_text(json.dumps(golden()) + "\n")
    adapter = tmp_path / "hang.py"
    adapter.write_text('import time\ntime.sleep(60)\n')
    result = run(vectors, shlex.join([sys.executable, str(adapter)]), .1)
    assert result["status"] == "execution_invalid"
    assert result["problems"][0]["code"] == "adapter_timeout"


def test_parser_semantic_json_and_raw_byte_contract_are_distinct():
    wanted = {"reasoning": "", "content": "", "tool_calls": [{"name": "x", "index": 0, "arguments": '{"a":1,"b":true}'}]}
    actual = copy.deepcopy(wanted)
    actual["tool_calls"][0]["arguments"] = '{ "b":true, "a":1 }'
    assert canonical_hash(comparison_value(actual, "json")) == canonical_hash(comparison_value(wanted, "json"))
    assert comparison_value(actual, "bytes") != comparison_value(wanted, "bytes")
    actual["tool_calls"][0]["arguments"] = '{"a":1,"b":1}'
    assert canonical_hash(comparison_value(actual, "json")) != canonical_hash(comparison_value(wanted, "json"))
    assert equivalent({"n": 1.0}, {"n": 1})
    assert not equivalent({"n": True}, {"n": 1})

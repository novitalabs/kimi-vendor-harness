from __future__ import annotations

import json

from novita_self_test.capability_runtime import Reply, assert_reply, parse_reply


def test_parse_stream_merges_tool_deltas_and_requires_done() -> None:
    first = {
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{
                "index": 0,
                "id": "get_weather_0",
                "function": {"name": "get_weather", "arguments": '{"city":"Bei'},
            }]},
        }]
    }
    second = {
        "choices": [{
            "index": 0,
            "delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'jing"}'}}]},
            "finish_reason": "tool_calls",
        }]
    }
    usage = {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}}
    body = "\n".join([f"data: {json.dumps(item)}" for item in (first, second, usage)]) + "\ndata: [DONE]"
    reply = parse_reply(200, body, stream=True)
    assert not reply.errors
    assert reply.done
    assert reply.calls[0]["arguments"] == '{"city":"Beijing"}'
    assert reply.finish_reason == "tool_calls"
    assert reply.usage == {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5}


def test_parse_stream_rejects_missing_done() -> None:
    reply = parse_reply(200, 'data: {"choices":[]}', stream=True)
    assert "missing DONE marker" in reply.errors


def test_assertion_distinguishes_invalid_request_from_server_failure() -> None:
    assert assert_reply(Reply(400, '{"error":{}}'), {"status": 400, "error_object": True}, []) == []
    assert assert_reply(Reply(500, "crashed"), {"status": 400, "error_object": True}, []) == [
        "status=500, expected=400"
    ]

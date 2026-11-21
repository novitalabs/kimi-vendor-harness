"""A small loopback-only OpenAI-compatible fixture with switchable faults.

The healthy fixture covers the default capability smoke, not the complete
catalog or any real engine. Faults are negative controls for harness assertions.

    python fake_k3_server.py --port 18080 [--faults empty_turn,xtml_leak]

Faults:
  schema_500        invalid tool schema -> 500 instead of 400
  usage_stream_off  streamed usage.prompt_tokens is off by one
  marker_special    literal marker text is counted as structural tokens
  empty_turn        tools present, tool_choice auto/required -> 200 with no content and no tool_calls
  xtml_leak         `<|close|>` leaks into content
  undeclared_tool   returns a call to a tool that was not declared
  bad_args          tool arguments are truncated, invalid JSON
  tools_override_rf with tools present, response_format is ignored
  json_empty_key    json_object + thinking answers {"": ...}
  ignore_parallel   parallel_tool_calls=false is ignored (observation only)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MARKERS = ("<|open|>", "<|close|>", "<|sep|>", "<|end_of_msg|>", "<|media_pad|>")
TOKEN_RE = re.compile(r"\w+|[^\w\s]")
VALID_TYPES = {"string", "number", "integer", "boolean", "object", "array", "null"}


def count_text(text: str, special: bool) -> int:
    extra = 0
    if special:
        for marker in MARKERS:
            extra += text.count(marker)
            text = text.replace(marker, " ")
    return len(TOKEN_RE.findall(text)) + extra


def content_text(content: Any) -> str:
    """Flatten OpenAI message content (a string or a list of parts) to its text parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text") or "" for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""


def prompt_tokens(req: dict[str, Any], faults: set[str]) -> int:
    special = "marker_special" in faults
    total = 3
    for message in req.get("messages") or []:
        total += count_text(content_text(message.get("content")), special)
        for call in message.get("tool_calls") or []:
            total += count_text(json.dumps(call.get("function") or {}, ensure_ascii=False), special)
    if req.get("tools"):
        total += count_text(json.dumps(req["tools"], ensure_ascii=False), special)
    return total


def schema_ok(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    kind = schema.get("type")
    if kind is not None and not (
        (isinstance(kind, str) and kind in VALID_TYPES)
        or (isinstance(kind, list) and all(k in VALID_TYPES for k in kind))
    ):
        return False
    props = schema.get("properties")
    if props is not None and (not isinstance(props, dict) or not all(schema_ok(v) for v in props.values())):
        return False
    if "required" in schema and not isinstance(schema["required"], list):
        return False
    if "enum" in schema and not isinstance(schema["enum"], list):
        return False
    return True


def thinking_on(req: dict[str, Any]) -> bool:
    if req.get("reasoning_effort") in ("low", "high", "max"):
        return True
    return (req.get("thinking") or {}).get("type") == "enabled"


def sample_args(tool: dict[str, Any], city: str, last_user: str) -> dict[str, Any]:
    params = (tool.get("function") or {}).get("parameters") or {}
    args: dict[str, Any] = {}
    for key in params.get("required") or []:
        spec = (params.get("properties") or {}).get(key) or {}
        if key == "content":
            args[key] = last_user.split("\n")[-1]
        elif spec.get("enum"):
            args[key] = spec["enum"][0]
        elif spec.get("type") == "number":
            args[key] = 1
        else:
            args[key] = city if key == "city" else "a.txt" if key == "path" else "1+1"
    return args


def value_for(spec: dict[str, Any]) -> Any:
    return {"string": "x", "number": 1, "integer": 1, "boolean": True}.get(spec.get("type"), None)


def compose(req: dict[str, Any], faults: set[str]) -> dict[str, Any]:
    tools = req.get("tools") or []
    choice = req.get("tool_choice") or "auto"
    messages = req.get("messages") or []
    last_user = next((content_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    result: dict[str, Any] = {"content": "", "reasoning": "思考中" if thinking_on(req) else "", "calls": []}

    wants_call = bool(tools) and choice != "none" and (choice == "required" or "天气" in last_user or "write_file" in last_user)
    if wants_call and "empty_turn" in faults:
        return result
    if wants_call:
        tool = next((t for t in tools if "write_file" in last_user and t["function"]["name"] == "write_file"), None)
        tool = tool or next((t for t in tools if "weather" in t["function"]["name"]), tools[0])
        cities = ["北京"]
        if "同时" in last_user and (req.get("parallel_tool_calls") is not False or "ignore_parallel" in faults):
            cities = ["北京", "上海"]
        for index, city in enumerate(cities):
            name = "do_unknown_tool" if "undeclared_tool" in faults else tool["function"]["name"]
            arguments = json.dumps(sample_args(tool, city, last_user), ensure_ascii=False)
            if "bad_args" in faults:
                arguments = arguments[: max(2, len(arguments) // 2)]
            result["calls"].append({"id": f"{name}_{index}", "name": name, "arguments": arguments})
    else:
        fmt = req.get("response_format") or {}
        if fmt.get("type") and fmt["type"] != "text" and not (tools and "tools_override_rf" in faults):
            if fmt["type"] == "json_schema":
                schema = (fmt.get("json_schema") or {}).get("schema") or {}
                body = {k: value_for(v) for k, v in (schema.get("properties") or {}).items()}
            else:
                body = {"": "ok"} if ("json_empty_key" in faults and thinking_on(req)) else {"result": "ok"}
            result["content"] = json.dumps(body, ensure_ascii=False)
        else:
            result["content"] = "好的，这是一个普通回答。"
    if "xtml_leak" in faults:
        result["content"] += "<|close|>message<|sep|>"
    return result


class Handler(BaseHTTPRequestHandler):
    faults: set[str] = set()

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "kimi-k3", "object": "model"}]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        for tool in req.get("tools") or []:
            if not schema_ok((tool.get("function") or {}).get("parameters", {})):
                status = 500 if "schema_500" in self.faults else 400
                return self._json(status, {"error": {"message": "invalid tool schema", "type": "invalid_request_error"}})
        out = compose(req, self.faults)
        stream = bool(req.get("stream"))
        used = prompt_tokens(req, self.faults) + (1 if stream and "usage_stream_off" in self.faults else 0)
        cap = req.get("max_completion_tokens", req.get("max_tokens", 5))
        completion = min(5, cap)
        usage = {"prompt_tokens": used, "completion_tokens": completion, "total_tokens": used + completion}
        finish = "length" if cap < 5 else "tool_calls" if out["calls"] else "stop"
        if not stream:
            message: dict[str, Any] = {"role": "assistant", "content": out["content"]}
            if out["reasoning"]:
                message["reasoning_content"] = out["reasoning"]
            if out["calls"]:
                message["tool_calls"] = [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}}
                    for c in out["calls"]
                ]
            return self._json(200, {"id": "chatcmpl-fake", "object": "chat.completion", "model": req.get("model"), "created": 0,
                                    "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})
        chunks: list[dict[str, Any]] = [{"role": "assistant", "content": ""}]
        if out["reasoning"]:
            chunks.append({"reasoning_content": out["reasoning"]})
        if out["content"]:
            half = len(out["content"]) // 2 or 1
            chunks += [{"content": out["content"][:half]}, {"content": out["content"][half:]}]
        for index, call in enumerate(out["calls"]):
            chunks.append({"tool_calls": [{"index": index, "id": call["id"], "type": "function",
                                           "function": {"name": call["name"], "arguments": ""}}]})
            half = len(call["arguments"]) // 2 or 1
            for part in (call["arguments"][:half], call["arguments"][half:]):
                chunks.append({"tool_calls": [{"index": index, "function": {"arguments": part}}]})
        frames = [{"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": req.get("model"), "created": 0, "choices": [{"index": 0, "delta": d, "finish_reason": None}]}
                  for d in chunks]
        frames.append({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": req.get("model"), "created": 0,
                       "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
        if (req.get("stream_options") or {}).get("include_usage"):
            frames.append({"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": req.get("model"), "created": 0, "choices": [], "usage": usage})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for frame in frames:
            self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.close_connection = True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--faults", default="")
    args = parser.parse_args()
    Handler.faults = {f for f in args.faults.split(",") if f}
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("ready", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        sys.exit(0)


if __name__ == "__main__":
    main()

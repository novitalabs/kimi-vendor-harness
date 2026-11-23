#!/usr/bin/env python3
"""Bridge to an approved local parser, not a Kimi parser or golden-output echo.

Use --function my_bridge:parse. parse(request) receives {id, text, chunks, tools,
finish}; reset parser state for EACH request, feed chunks in order, then finish.
Return {reasoning: str, content: str, tool_calls: [{name, index, arguments: str}]}.
The import/function is explicitly developer-selected, never response-derived.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--function", required=True, help="approved module:function")
    args = parser.parse_args()
    module, name = args.function.split(":", 1)
    parse = getattr(importlib.import_module(module), name)
    for line in sys.stdin:
        try:
            request = json.loads(line)
            result = parse(request)
            if not isinstance(result, dict) or not {"reasoning", "content", "tool_calls"} <= result.keys():
                raise ValueError("parser needs reasoning, content and tool_calls")
            print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
        except (ValueError, TypeError, KeyError) as exc:
            print(f"parser adapter input/output invalid: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

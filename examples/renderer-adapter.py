#!/usr/bin/env python3
"""JSONL bridge to a developer-approved local renderer, NOT a Kimi renderer.

Usage: kimi-harness renderer run --vectors /pinned/goldens.jsonl \
  --adapter 'python examples/renderer-adapter.py --function my_bridge:render'

my_bridge.render(request: dict) -> list[int] must call the actual framework
renderer/tokenizer. Use the checkpoint encoder separately to create the goldens;
never use this same implementation as both authority and candidate. The function
name is a developer-selected command-line argument, never model output.
"""
from __future__ import annotations

import argparse
import importlib
import json
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--function", required=True, help="approved importable module:function")
    args = parser.parse_args()
    module, name = args.function.split(":", 1)
    render = getattr(importlib.import_module(module), name)
    for line in sys.stdin:
        try:
            value = json.loads(line)
            tokens = render(value["request"])
            if not isinstance(tokens, list) or any(type(token) is not int or token < 0 for token in tokens):
                raise ValueError("renderer must return a list of nonnegative integer token ids")
            print(json.dumps({"id": value["id"], "token_ids": tokens}), flush=True)
        except (ValueError, KeyError, TypeError) as exc:
            print(f"renderer adapter input/output invalid: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc


if __name__ == "__main__":
    main()

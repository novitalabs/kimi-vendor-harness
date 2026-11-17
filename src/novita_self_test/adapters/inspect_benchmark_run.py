from __future__ import annotations

import argparse

from inspect_ai import eval


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run pinned KVV Inspect tasks with explicit sample waves")
    parser.add_argument("benchmark", choices=("ocrbench", "mmmu"))
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tokens", required=True, type=int)
    parser.add_argument("--thinking", action="store_true")
    parser.add_argument("--think-mode", choices=("none", "kimi", "opensource"), default="none")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--max-connections", type=int, default=50)
    parser.add_argument("--temperature", type=float, default=1.0)
    parser.add_argument("--top-p", type=float, default=0.95)
    parser.add_argument("--thinking-effort")
    parser.add_argument("--client-timeout", type=int, default=86400)
    parser.add_argument("--sample-id", action="append", default=[])
    parser.add_argument("--retry-on-error", type=int, default=3)
    args = parser.parse_args(argv)

    import kimi_model  # noqa: F401 - registers KVV model providers
    from eval import get_thinking_extra_body
    from mmmu_pro_vision import mmmu_pro_10c
    from ocr_bench import ocrbench

    task = ocrbench if args.benchmark == "ocrbench" else mmmu_pro_10c
    eval(
        [task],
        [args.model],
        max_tokens=args.max_tokens,
        max_connections=args.max_connections,
        epochs=1,
        extra_body=get_thinking_extra_body(
            args.thinking, args.think_mode, args.thinking_effort
        ),
        retry_on_error=args.retry_on_error,
        continue_on_fail=True,
        fail_on_error=False,
        sample_id=args.sample_id or None,
        temperature=args.temperature,
        top_p=args.top_p,
        model_args={
            "stream": args.stream,
            "max_retries": 0,
            "timeout": args.client_timeout,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

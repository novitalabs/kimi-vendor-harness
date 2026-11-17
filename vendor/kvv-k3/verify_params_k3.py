"""
Verify Kimi-K3 API parameter constraints before running benchmarks.

Immutable parameters (must use default values):
- temperature: 1.0 (think) / omitted (non-think)
- top_p: 0.95 (think) / omitted (non-think)
- presence_penalty: 0
- frequency_penalty: 0
- n: 1

Thinking defaults:
- keep: all
- effort: max
"""

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx
from openai import BadRequestError, OpenAI


@dataclass
class ParamSpec:
    name: str
    think_default: Any
    non_think_default: Any
    wrong_value: Any


IMMUTABLE_PARAMS: list[ParamSpec] = [
    ParamSpec("temperature", 1.0, None, 1.1),
    ParamSpec("temperature", 1.0, None, 2.0),
    ParamSpec("temperature", 1.0, None, -0.1),
    ParamSpec("top_p", 0.95, None, 0.8),
    ParamSpec("presence_penalty", 0, 0, 0.5),
    ParamSpec("frequency_penalty", 0, 0, 0.5),
    ParamSpec("n", 1, 1, 2),
]

DEFAULT_THINKING_KEEP = "all"
DEFAULT_THINKING_EFFORT = "max"


def get_client(base_url: str, api_key: str) -> OpenAI:
    return OpenAI(
        base_url=base_url,
        api_key=api_key,
        http_client=httpx.Client(timeout=60.0),
    )


def get_thinking_extra_body(
    thinking: bool,
    think_mode: str,
    thinking_keep: str | None = None,
    thinking_effort: str | None = None,
) -> dict:
    if think_mode == "opensource":
        chat_template_kwargs = {"thinking": thinking}
        if thinking:
            chat_template_kwargs["preserve_thinking"] = True
            chat_template_kwargs["thinking_effort"] = thinking_effort or DEFAULT_THINKING_EFFORT
        return {"chat_template_kwargs": chat_template_kwargs}

    thinking_body = {"type": "enabled" if thinking else "disabled"}
    if thinking:
        thinking_body["keep"] = thinking_keep or DEFAULT_THINKING_KEEP
        thinking_body["effort"] = thinking_effort or DEFAULT_THINKING_EFFORT
    return {"thinking": thinking_body}


def summarize_message(response: Any) -> str:
    if not response.choices:
        return "no choices"

    message = response.choices[0].message
    content = message.content or ""
    if content:
        return content[:50]

    reasoning_content = getattr(message, "reasoning_content", None) or ""
    if reasoning_content:
        return f"reasoning_content: {reasoning_content[:50]}"

    return "empty message"


def make_request(
    client: OpenAI,
    model: str,
    thinking: bool,
    think_mode: str = "kimi",
    extra_params: dict | None = None,
    thinking_keep: str | None = None,
    thinking_effort: str | None = None,
    extra_body_override: dict | None = None,
) -> tuple[bool, str]:
    """Send test request. Returns (success, message)."""
    kwargs = {
        "model": model,
        "messages": [{"role": "user", "content": "Say 'OK' and nothing else."}],
        "extra_body": (
            extra_body_override
            if extra_body_override is not None
            else get_thinking_extra_body(
                thinking,
                think_mode,
                thinking_keep=thinking_keep,
                thinking_effort=thinking_effort,
            )
        ),
    }
    if extra_params:
        kwargs.update(extra_params)

    try:
        response = client.chat.completions.create(**kwargs)
        return True, f"OK: {summarize_message(response)}"
    except BadRequestError as e:
        return False, f"Rejected(400): {e.message}"
    except Exception as e:
        return False, f"Error: {type(e).__name__}: {e}"


def test_param_rejected(
    client: OpenAI,
    model: str,
    thinking: bool,
    param: ParamSpec,
    think_mode: str = "kimi",
) -> tuple[bool, str]:
    """Test that wrong param value is rejected."""
    default_value = param.think_default if thinking else param.non_think_default
    if param.wrong_value == default_value:
        return True, "Skip: wrong_value == default_value"

    success, _ = make_request(client, model, thinking, think_mode, {param.name: param.wrong_value})
    if success:
        return False, f"❌ FAIL: {param.name}={param.wrong_value} should be rejected"
    return True, f"✓ PASS: {param.name}={param.wrong_value} correctly rejected"


def test_param_accepted(
    client: OpenAI,
    model: str,
    thinking: bool,
    param: ParamSpec,
    think_mode: str = "kimi",
) -> tuple[bool, str]:
    """Test that correct param value is accepted."""
    default_value = param.think_default if thinking else param.non_think_default
    if default_value is None:
        return True, f"Skip: {param.name} default is omitted in this mode"

    success, msg = make_request(client, model, thinking, think_mode, {param.name: default_value})
    if success:
        return True, f"✓ PASS: {param.name}={default_value} accepted"
    return False, f"❌ FAIL: {param.name}={default_value} rejected: {msg}"


def test_no_param(
    client: OpenAI,
    model: str,
    thinking: bool,
    think_mode: str = "kimi",
) -> tuple[bool, str]:
    """Test request without optional params."""
    success, msg = make_request(client, model, thinking, think_mode)
    if success:
        return True, "✓ PASS: request without params succeeded"
    return False, f"❌ FAIL: request without params failed: {msg}"


def test_thinking_defaults(
    client: OpenAI,
    model: str,
    think_mode: str = "kimi",
) -> list[tuple[bool, str]]:
    """Verify K3 thinking can omit keep/effort and accepts explicit defaults."""
    results = []

    success, msg = make_request(client, model, True, think_mode)
    if success:
        results.append((True, "✓ PASS: default keep=all and effort=max accepted"))
    else:
        results.append(
            (
                False,
                "❌ FAIL: default keep=all and effort=max rejected: "
                f"{msg}",
            )
        )

    if think_mode == "kimi":
        success, msg = make_request(
            client,
            model,
            True,
            think_mode,
            thinking_keep="all",
            thinking_effort="max",
        )
        if success:
            results.append((True, "✓ PASS: explicit thinking.keep=all and effort=max accepted"))
        else:
            results.append(
                (
                    False,
                    "❌ FAIL: explicit thinking.keep=all and effort=max rejected: "
                    f"{msg}",
                )
            )
    else:
        success, msg = make_request(
            client,
            model,
            True,
            think_mode,
            thinking_effort="max",
        )
        if success:
            results.append((True, "✓ PASS: explicit thinking_effort=max accepted"))
        else:
            results.append((False, f"❌ FAIL: explicit thinking_effort=max rejected: {msg}"))

    return results


def test_disabled_preserve_thinking_rejected(
    client: OpenAI,
    model: str,
    think_mode: str = "kimi",
) -> tuple[bool, str]:
    """Verify disabled thinking cannot request preserved historical thinking."""
    if think_mode == "kimi":
        extra_body = {"thinking": {"type": "disabled", "keep": DEFAULT_THINKING_KEEP}}
        label = "thinking.type=disabled with keep=all"
    else:
        extra_body = {
            "chat_template_kwargs": {
                "thinking": False,
                "preserve_thinking": True,
                "thinking_effort": DEFAULT_THINKING_EFFORT,
            }
        }
        label = "thinking=false with preserve_thinking=true"

    success, _ = make_request(
        client,
        model,
        False,
        think_mode,
        extra_body_override=extra_body,
    )
    if success:
        return False, f"❌ FAIL: {label} should be rejected"
    return True, f"✓ PASS: {label} correctly rejected"


def run_verification(
    base_url: str,
    api_key: str,
    model: str,
    thinking: bool,
    think_mode: str = "kimi",
    test_reject: bool = True,
    test_accept: bool = True,
) -> bool:
    """Run full verification. Returns True if all tests pass."""
    client = get_client(base_url, api_key)
    mode_str = "think" if thinking else "non-think"

    print(f"\n{'='*60}")
    print(f"Mode: {mode_str} (think-mode: {think_mode})")
    print(f"Model: {model}")
    print(f"Base URL: {base_url}")
    print(f"{'='*60}\n")

    all_passed = True
    results = []

    # Test 1: no optional params
    print("[1] Test without optional params...")
    passed, msg = test_no_param(client, model, thinking, think_mode)
    results.append(passed)
    if not passed:
        all_passed = False
    print(f"    {msg}")

    if thinking:
        print("\n[2] Test K3 thinking keep/effort defaults...")
        for passed, msg in test_thinking_defaults(client, model, think_mode):
            results.append(passed)
            if not passed:
                all_passed = False
            print(f"    {msg}")
    else:
        print("\n[2] Test disabled thinking rejects preservation...")
        passed, msg = test_disabled_preserve_thinking_rejected(client, model, think_mode)
        results.append(passed)
        if not passed:
            all_passed = False
        print(f"    {msg}")

    # Test 3: correct default values accepted
    if test_accept:
        print("\n[3] Test correct default values...")
        for param in IMMUTABLE_PARAMS:
            passed, msg = test_param_accepted(client, model, thinking, param, think_mode)
            results.append(passed)
            if not passed:
                all_passed = False
            print(f"    {msg}")

    # Test 4: wrong values rejected
    if test_reject:
        print("\n[4] Test wrong values (should be rejected)...")
        for param in IMMUTABLE_PARAMS:
            passed, msg = test_param_rejected(client, model, thinking, param, think_mode)
            results.append(passed)
            if not passed:
                all_passed = False
            print(f"    {msg}")

    # Summary
    print(f"\n{'='*60}")
    passed_count = sum(results)
    status = "✓ ALL PASSED" if all_passed else "❌ SOME FAILED"
    print(f"Result: {status} ({passed_count}/{len(results)})")
    print(f"{'='*60}\n")

    return all_passed


def main():
    parser = argparse.ArgumentParser(
        description="Verify K3 API parameter constraints",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python verify_params_k3.py --model k3 --thinking --think-mode kimi
  python verify_params_k3.py --model k3 --think-mode opensource --base-url http://localhost:8000/v1
  python verify_params_k3.py --model k3 --all
""",
    )
    parser.add_argument("--model", required=True, help="Model name")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("KIMI_BASE_URL", "https://api.moonshot.cn/v1"),
        help="API base URL (default: $KIMI_BASE_URL or https://api.moonshot.cn/v1)",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("KIMI_API_KEY"),
        help="API key (default: $KIMI_API_KEY)",
    )
    parser.add_argument("--thinking", action="store_true", help="Verify thinking mode")
    parser.add_argument(
        "--think-mode",
        choices=["kimi", "opensource"],
        default="kimi",
        help="Thinking param format: kimi (SaaS) or opensource (vLLM/SGLang/KTransformers)",
    )
    parser.add_argument("--only-reject", action="store_true", help="Only test wrong values rejected")
    parser.add_argument("--only-accept", action="store_true", help="Only test correct values accepted")
    parser.add_argument("--all", action="store_true", help="Verify both thinking and non-thinking modes")

    args = parser.parse_args()

    if not args.api_key:
        print("Error: Set KIMI_API_KEY env var or use --api-key")
        sys.exit(1)

    test_reject = not args.only_accept
    test_accept = not args.only_reject
    modes = [False, True] if args.all else [args.thinking]

    all_passed = True
    for thinking in modes:
        if not run_verification(
            args.base_url,
            args.api_key,
            args.model,
            thinking,
            think_mode=args.think_mode,
            test_reject=test_reject,
            test_accept=test_accept,
        ):
            all_passed = False

    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()

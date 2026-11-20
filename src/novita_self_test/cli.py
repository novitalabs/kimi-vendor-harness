"""The single public CLI for endpoint cases and offline parser conformance."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .case_catalog import build_plan, load_cases, select_cases
from .capability_runtime import run_plan, token_parameter
from .config import ConfigError, ServiceAccess
from .parser_conformance import DEFAULT_VECTORS, run_adapter, validate_vectors


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="kimi-harness", description="Kimi capability and regression tests", allow_abbrev=False)
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="inspect cases and request budgets (no traffic)", allow_abbrev=False)
    plan.add_argument("--model", default="kimi-k3")
    plan.add_argument("--select", action="append")
    plan.add_argument("--repeat", type=int, default=1)
    plan.add_argument("--format", choices=("json", "summary"), default="json")
    plan.add_argument("--out", type=Path)

    doctor = commands.add_parser("doctor", help="zero-traffic local preflight; does not prove endpoint health", allow_abbrev=False)
    doctor.add_argument("--model", default="kimi-k3")
    doctor.add_argument("--service-access", type=Path)
    doctor.add_argument("--select", action="append")
    doctor.add_argument("--max-requests", type=int, default=32)

    run = commands.add_parser("run", help="run selected cases against an endpoint", allow_abbrev=False)
    run.add_argument("--model", default="kimi-k3")
    run.add_argument("--service-access", required=True, type=Path)
    run.add_argument("--select", action="append")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--max-requests", type=int, default=32)
    run.add_argument("--timeout", type=float, default=120, help="wall-clock seconds per HTTP request")
    run.add_argument("--execute", action="store_true", help="acknowledge endpoint traffic")
    run.add_argument("--allow-opt-in", action="store_true", help="also acknowledge explicitly tagged opt-in/long-history cases")

    conformance = commands.add_parser("parser", help="offline parser-vector validation and adapter execution", allow_abbrev=False)
    sub = conformance.add_subparsers(dest="parser_command", required=True)
    validate = sub.add_parser("validate", help="validate vector files only; no adapter or endpoint", allow_abbrev=False)
    validate.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    adapter = sub.add_parser("run", help="run vectors through a framework-supplied adapter (no endpoint)", allow_abbrev=False)
    adapter.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    adapter.add_argument("--adapter", required=True, help="executable command consuming/emitting JSONL")
    adapter.add_argument("--timeout", type=float, default=30.0, help="total adapter wall-clock seconds")
    return root


def _plan(model: str, selectors: list[str] | None, repeat: int = 1) -> dict:
    return build_plan(select_cases(load_cases(), selectors or ["smoke"]), model=model, repeat=repeat)


def _summary(plan: dict) -> str:
    lines = [f"Model: {plan['model']} ({plan['profile_status']})",
             f"Cases: {len(plan['cases'])}; requests: {plan['request_budget']}; output-token ceiling: {plan['max_output_tokens_budget']}",
             f"Repeat: {plan['repeat']}; concurrency: {plan['concurrency']}; retries: {plan['retries']}",
             "No endpoint traffic generated.", ""]
    for case in plan["cases"]:
        applicability = "unsupported: " + ", ".join(case["missing_requirements"]) if case["missing_requirements"] else "applicable"
        opt_in = "; requires --allow-opt-in" if "opt_in" in case["tags"] else ""
        lines.append(f"- {case['id']} | {case['planned_requests']} requests | {applicability}{opt_in}")
        lines.append(f"  {case['title']} [{case['determinism']}]")
    return "\n".join(lines)


def _access(path: Path, model: str, *, credential_required: bool) -> ServiceAccess:
    access = ServiceAccess.load(path)
    if access.model_family != model:
        raise ConfigError("model_mismatch", f"service model_family={access.model_family!r} does not match {model!r}")
    if credential_required and not access.credential():
        raise ConfigError("credential_missing", f"set {access.api_key_env}")
    return access


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "parser":
            if args.parser_command == "validate":
                return validate_vectors(args.vectors)
            return run_adapter(args.vectors, args.adapter, timeout=args.timeout)
        if args.command == "plan":
            plan = _plan(args.model, args.select, args.repeat)
            text = _summary(plan) if args.format == "summary" else json.dumps(plan, ensure_ascii=False, indent=2)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(text + "\n", encoding="utf-8")
            print(text)
            return 0
        if args.command == "run":
            if not args.execute:
                raise ConfigError("execute_ack_required", "run sends endpoint traffic; add --execute")
            plan = _plan(args.model, args.select, args.repeat)
            access = _access(args.service_access, args.model, credential_required=plan["request_budget"] > 0)
            output = run_plan(plan, access, max_requests=args.max_requests, timeout=args.timeout, allow_opt_in=args.allow_opt_in)
            exit_code = 0 if output["status"] == "passed" else 1 if output["status"] == "failed" else 2
        elif args.command == "doctor":
            if args.max_requests < 0:
                raise ConfigError("request_budget_invalid", "max_requests must be nonnegative")
            plan = _plan(args.model, args.select)
            token_parameter()
            if args.service_access:
                _access(args.service_access, args.model, credential_required=plan["request_budget"] > 0)
            unsupported = [{"id": case["id"], "missing_requirements": case["missing_requirements"]}
                           for case in plan["cases"] if case["missing_requirements"]]
            reasons = []
            if unsupported:
                reasons.append("selected_cases_unsupported")
            if plan["request_budget"] > args.max_requests:
                reasons.append("request_budget_exceeds_ceiling")
            output = {"status": "blocked" if reasons else "passed", "model": args.model,
                      "profile_status": plan["profile_status"], "case_count": len(plan["cases"]),
                      "planned_requests": plan["request_budget"], "max_requests": args.max_requests,
                      "unsupported": unsupported, "reasons": reasons, "endpoint_probe": False,
                      "access_checked": args.service_access is not None,
                      "message": "Local preflight only; no endpoint traffic or endpoint health check."}
            exit_code = 2 if reasons else 0
        else:
            raise ConfigError("command_invalid", "unsupported command")
    except ConfigError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return exit_code

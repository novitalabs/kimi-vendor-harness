"""Kimi agentic capability harness command line."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .case_catalog import build_plan, load_cases, load_profile, select_cases
from .capability_runtime import run_plan
from .config import ConfigError, ServiceAccess, workspace_root


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="kimi-harness", description="Kimi capability and regression tests")
    commands = root.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="list capability cases")
    listing.add_argument("--model", default="kimi-k3")
    listing.add_argument("--select", action="append")

    capabilities = commands.add_parser("capabilities", help="plan or run capability cases")
    sub = capabilities.add_subparsers(dest="capability_command", required=True)
    plan = sub.add_parser("plan", help="zero-traffic plan")
    plan.add_argument("--model", default="kimi-k3")
    plan.add_argument("--select", action="append")
    plan.add_argument("--repeat", type=int, default=1)
    plan.add_argument("--out", type=Path)
    run = sub.add_parser("run", help="run endpoint capability cases")
    run.add_argument("--model", default="kimi-k3")
    run.add_argument("--service-access", required=True, type=Path)
    run.add_argument("--select", action="append")
    run.add_argument("--repeat", type=int, default=1)
    run.add_argument("--max-requests", type=int, default=32)
    run.add_argument("--timeout", type=float, default=120)
    run.add_argument("--execute", action="store_true", help="acknowledge endpoint traffic")

    doctor = commands.add_parser("doctor", help="zero-traffic local capability preflight")
    doctor.add_argument("--model", default="kimi-k3")
    doctor.add_argument("--service-access", type=Path)
    doctor.add_argument("--select", action="append")
    doctor.add_argument("--max-requests", type=int, default=32)
    return root


def _plan(model: str, selectors: list[str] | None, repeat: int) -> dict:
    cases = select_cases(load_cases(), selectors or ["smoke"])
    return build_plan(cases, model=model, repeat=repeat)


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "list":
            profile = load_profile(args.model)
            cases = select_cases(load_cases(), args.select)
            output = {"model": args.model, "profile_status": profile["status"], "cases": [{key: case[key] for key in ("id", "title", "capability", "scope", "tags", "determinism", "request_budget", "likely_components", "knowledge")} for case in cases]}
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        if args.command == "capabilities" and args.capability_command == "plan":
            output = _plan(args.model, args.select, args.repeat)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0
        if args.command == "capabilities" and args.capability_command == "run":
            if not args.execute:
                raise ConfigError("execute_ack_required", "capability run sends endpoint traffic; add --execute")
            service = ServiceAccess.load(args.service_access)
            if service.model_family != args.model:
                raise ConfigError("model_mismatch", f"service model_family={service.model_family!r} does not match {args.model!r}")
            output = run_plan(_plan(args.model, args.select, args.repeat), service, max_requests=args.max_requests, timeout=args.timeout)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if output["status"] == "passed" else 2 if output["status"] in {"service_invalid", "execution_invalid", "incomplete"} else 1
        if args.command == "doctor":
            if args.service_access:
                access = ServiceAccess.load(args.service_access)
                if not access.credential():
                    raise ConfigError("credential_missing", f"set {access.api_key_env}")
            cases = select_cases(load_cases(), args.select or ["smoke"])
            profile = load_profile(args.model)
            plan = build_plan(cases, model=args.model)
            output = {"status": "passed", "model": args.model, "profile_status": profile["status"],
                      "case_count": len(cases), "planned_requests": plan["request_budget"],
                      "max_requests": args.max_requests, "endpoint_probe": False,
                      "message": "No endpoint traffic was generated."}
            if plan["request_budget"] > args.max_requests:
                output.update(status="blocked", reason="request_budget_exceeds_ceiling")
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if output["status"] == "passed" else 2
        raise ConfigError("command_invalid", "unsupported command")
    except ConfigError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False), file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

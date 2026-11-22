"""One machine-readable CLI; explicit summary/help are human-readable views."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from .capability_runtime import run_plan
from .case_catalog import load_cases
from .config import ConfigError, ServiceAccess, redact
from .contracts import error_result, exit_code, issue, json_text, validate_contract
from .parser_conformance import DEFAULT_VECTORS, run_adapter, validate_vectors
from .execution_plan import create_plan, load_plan, verify_plan
from .renderer_conformance import run as run_renderer
from .report_comparison import compare, load_report, failed_ids


class ArgumentParser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, allow_abbrev=False, **kwargs)

    def error(self, message: str) -> None:
        raise ConfigError("arguments_invalid", message)


def parser() -> argparse.ArgumentParser:
    root = ArgumentParser(prog="kimi-harness", description="Kimi capability and regression tests")
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="inspect cases and request budgets (no traffic)")
    plan.add_argument("--model", default="kimi-k3")
    plan.add_argument("--select", action="append")
    plan.add_argument("--failed-from", type=Path, help="replan whole failed/inconclusive/not-run scenarios from a previous report; no automatic execution")
    plan.add_argument("--repeat", type=int, default=1)
    plan.add_argument("--format", choices=("json", "summary"), default="json")
    plan.add_argument("--out", type=Path)
    plan.add_argument("--service-access", type=Path, help="bind a public target identity to make the plan executable")
    plan.add_argument("--max-requests", type=int, default=32)
    plan.add_argument("--timeout", type=float, default=120, help="freeze wall-clock seconds per HTTP request")

    doctor = commands.add_parser("doctor", help="zero-traffic local preflight; does not prove endpoint health")
    doctor.add_argument("--model")
    doctor.add_argument("--service-access", type=Path)
    doctor.add_argument("--select", action="append")
    doctor.add_argument("--max-requests", type=int)
    doctor.add_argument("--plan", type=Path, help="verify a frozen plan, with no traffic")

    run = commands.add_parser("run", help="run selected cases against an endpoint")
    run.add_argument("--model")
    run.add_argument("--plan", type=Path, help="execute the previously reviewed frozen plan")
    run.add_argument("--service-access", required=True, type=Path)
    run.add_argument("--select", action="append")
    run.add_argument("--repeat", type=int)
    run.add_argument("--max-requests", type=int)
    run.add_argument("--timeout", type=float, help="wall-clock seconds per HTTP request; cannot override a saved plan")
    run.add_argument("--execute", action="store_true", help="acknowledge endpoint traffic")
    run.add_argument("--allow-opt-in", action="store_true", help="also acknowledge explicitly tagged opt-in/long-history cases")

    conformance = commands.add_parser("parser", help="offline parser-vector validation and adapter execution")
    sub = conformance.add_subparsers(dest="parser_command", required=True)
    validate = sub.add_parser("validate", help="validate vector files only; no adapter or endpoint")
    validate.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    adapter = sub.add_parser("run", help="run vectors through a framework-supplied adapter (no endpoint)")
    adapter.add_argument("--vectors", type=Path, default=DEFAULT_VECTORS)
    adapter.add_argument("--adapter", required=True, help="executable command consuming/emitting JSONL")
    adapter.add_argument("--timeout", type=float, default=30.0, help="total adapter wall-clock seconds")
    renderer = commands.add_parser("renderer", help="offline checkpoint-golden token-id validation/parity")
    renderer_sub = renderer.add_subparsers(dest="renderer_command", required=True)
    for action in ("validate", "run"):
        entry = renderer_sub.add_parser(action)
        entry.add_argument("--vectors", required=True, type=Path, help="provenance-pinned JSONL goldens supplied by the developer")
        if action == "run":
            entry.add_argument("--adapter", required=True)
            entry.add_argument("--timeout", type=float, default=30)
    comparison = commands.add_parser("compare", help="compare two endpoint reports offline without sending requests")
    comparison.add_argument("--baseline", required=True, type=Path)
    comparison.add_argument("--candidate", required=True, type=Path)
    return root


def _saved_plan(args, service: ServiceAccess) -> dict:
    if args.model is not None or args.select is not None or getattr(args, "repeat", None) is not None or getattr(args, "timeout", None) is not None:
        raise ConfigError("plan_override_invalid", "--plan cannot be combined with model/select/repeat/timeout overrides")
    plan = load_plan(args.plan)
    verify_plan(plan, service)
    if args.max_requests is not None and args.max_requests > plan["execution"]["max_requests"]:
        raise ConfigError("plan_override_invalid", "request ceiling cannot exceed the reviewed plan")
    return plan


def _summary(plan: dict) -> str:
    lines = [f"Model: {plan['model']} ({plan['profile_status']})",
             f"Cases: {len(plan['cases'])}; requests: {plan['request_budget']}; output-token ceiling: {plan['max_output_tokens_budget']}",
             f"Repeat: {plan['repeat']}; concurrency: {plan['concurrency']}; retries: {plan['retries']}",
             "Target-bound executable plan." if plan["executable"] else "Not executable: bind a target and resolve unknown requirements.",
             f"Request ceiling: {plan['execution']['max_requests']}; opt-in cases: {len(plan['opt_in_cases'])}",
             "No endpoint traffic generated.", ""]
    for case in plan["cases"]:
        applicability = "blocked: " + ", ".join(case["missing_requirements"]) if case["missing_requirements"] else "applicable"
        opt_in = "; requires --allow-opt-in" if "opt_in" in case["tags"] else ""
        lines.append(f"- {case['id']} | {case['planned_requests']} requests | {applicability}{opt_in}")
        lines.append(f"  {case['title']} [{case['role']}/{case['stage']}; {case['obligation']}; {case['determinism']}]")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    command, secret, execution_started = "cli", "", False
    try:
        args = parser().parse_args(argv)
        command = args.command + (" " + args.parser_command if args.command == "parser" else " " + args.renderer_command if args.command == "renderer" else "")
        if args.command == "parser":
            output = validate_vectors(args.vectors) if args.parser_command == "validate" else run_adapter(args.vectors, args.adapter, timeout=args.timeout)
            validate_contract(output, "parser-report.schema.json")
        elif args.command == "renderer":
            output = run_renderer(args.vectors, getattr(args, "adapter", None), getattr(args, "timeout", 30))
            validate_contract(output, "renderer-report.schema.json")
        elif args.command == "compare":
            output = compare(load_report(args.baseline), load_report(args.candidate))
            validate_contract(output, "comparison.schema.json")
        elif args.command == "plan":
            if args.failed_from:
                if args.select:
                    raise ConfigError("arguments_invalid", "--failed-from cannot be combined with --select")
                previous = load_report(args.failed_from)
                if previous["model"] != args.model:
                    raise ConfigError("model_mismatch", "failed report model differs; do not silently migrate test specifications")
                args.select = failed_ids(previous)
                if not args.select:
                    raise ConfigError("retest_empty", "report has no failed/inconclusive/not-run scenarios; no plan generated")
                known_ids = {case["id"] for case in load_cases()}
                if not set(args.select) <= known_ids:
                    raise ConfigError("retest_case_unknown", "report references case ids absent from the current catalog; review before replanning")
            service = ServiceAccess.load(args.service_access) if args.service_access else None
            plan = create_plan(model=args.model, selectors=args.select, repeat=args.repeat,
                               service=service, max_requests=args.max_requests, timeout=args.timeout)
            text = _summary(plan) if args.format == "summary" else json_text(plan)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(text, encoding="utf-8")
            print(text, end="")
            return 0
        elif args.command == "run":
            if not args.execute:
                raise ConfigError("execute_ack_required", "run sends endpoint traffic; add --execute")
            access = ServiceAccess.load(args.service_access)
            secret = access.credential()
            if args.plan:
                plan = _saved_plan(args, access)
            else:
                plan = create_plan(model=args.model or "kimi-k3", selectors=args.select,
                                   repeat=args.repeat if args.repeat is not None else 1,
                                   service=access, max_requests=args.max_requests if args.max_requests is not None else 32,
                                   timeout=args.timeout if args.timeout is not None else 120)
            ceiling = args.max_requests if args.max_requests is not None else plan["execution"]["max_requests"]
            execution_started = True
            output = run_plan(plan, access, max_requests=ceiling, timeout=plan["execution"]["timeout_seconds"], allow_opt_in=args.allow_opt_in)
        elif args.command == "doctor":
            if args.max_requests is not None and args.max_requests < 0:
                raise ConfigError("request_budget_invalid", "max_requests must be nonnegative")
            if args.plan:
                if not args.service_access:
                    raise ConfigError("plan_target_required", "doctor --plan requires --service-access")
                access = ServiceAccess.load(args.service_access)
                secret = access.credential()
                plan = _saved_plan(args, access)
            else:
                access = ServiceAccess.load(args.service_access) if args.service_access else None
                plan = create_plan(model=args.model or "kimi-k3", selectors=args.select, service=access,
                                   max_requests=args.max_requests if args.max_requests is not None else 32)
                if access:
                    secret = access.credential()
            ceiling = args.max_requests if args.max_requests is not None else plan["execution"]["max_requests"]
            if args.service_access and plan["request_budget"] and not secret:
                raise ConfigError("credential_missing", f"set {access.api_key_env}")
            unsupported = [{"id": case["id"], "missing_requirements": case["missing_requirements"]}
                           for case in plan["cases"] if case["missing_requirements"]]
            problems = []
            if unsupported:
                problems.append(issue("selected_cases_unsupported", "selected cases are not applicable", actual=unsupported))
            if plan["request_budget"] > ceiling:
                problems.append(issue("request_budget_exceeds_ceiling", "plan exceeds request ceiling", expected=ceiling, actual=plan["request_budget"]))
            output = {"schema_version": 1, "kind": "doctor", "status": "blocked" if problems else "passed", "model": plan["model"],
                      "case_count": len(plan["cases"]), "planned_requests": plan["request_budget"], "max_requests": ceiling,
                      "unsupported": unsupported, "problems": problems, "endpoint_probe": False,
                      "access_checked": args.service_access is not None, "plan_sha256": plan["plan_sha256"]}
            validate_contract(output, "doctor.schema.json")
        else:
            raise ConfigError("command_invalid", "unsupported command")
    except ConfigError as exc:
        # Output-contract errors can occur after HTTP; never claim zero traffic.
        count = None if execution_started and exc.code == "output_contract_invalid" else 0
        output = error_result(command, exc.code, str(exc), endpoint_requests=count)
    except (OSError, ValueError) as exc:
        output = error_result(command, "local_io_invalid", f"{type(exc).__name__}: {exc}",
                              endpoint_requests=None if execution_started else 0)
    except Exception as exc:
        output = error_result(command, "runner_error", f"{type(exc).__name__}: {exc}",
                              endpoint_requests=None if execution_started else 0)
    output = redact(output, secret)
    if output.get("kind") == "error":
        try:
            validate_contract(output, "error.schema.json")
        except Exception:
            # A broken local schema must not turn an error into missing stdout.
            output = error_result(command, "output_contract_invalid", "cannot validate error result with local schemas",
                                  endpoint_requests=output["endpoint_requests"])
    print(json_text(output), end="")
    return exit_code(output)

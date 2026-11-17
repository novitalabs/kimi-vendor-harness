from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .config import ConfigError, ModelProfile
from .collection import write_collection
from .doctor import run_doctor
from .orchestrator import Orchestrator, UserInterrupted
from .reporting import regenerate_report
from .requirements import refresh_requirements
from .session import continue_session, create_session, report_session


def _long_values(values: list[str] | None) -> set[str]:
    result = set()
    for value in values or []:
        result.update(item.strip() for item in value.split(",") if item.strip())
    return result


def _deepswe_supplemental_plan(args: argparse.Namespace) -> dict[str, object] | None:
    source_run = getattr(args, "deepswe_supplemental_source_run", None)
    pair_values = list(getattr(args, "deepswe_supplemental_pair", None) or [])
    if not source_run and not pair_values:
        return None
    if not source_run or not pair_values:
        raise ConfigError(
            "deepswe_supplemental_invalid",
            "supplemental recovery requires both --deepswe-supplemental-source-run and --deepswe-supplemental-pair",
        )
    pairs: list[dict[str, object]] = []
    observed: set[tuple[int, str]] = set()
    for value in pair_values:
        round_text, separator, task = value.partition(":")
        if not separator or not round_text.isdigit() or not task:
            raise ConfigError(
                "deepswe_supplemental_invalid",
                f"invalid round/task pair {value!r}; expected ROUND:TASK",
            )
        pair = (int(round_text), task)
        if pair in observed:
            raise ConfigError("deepswe_supplemental_invalid", f"duplicate round/task pair: {value}")
        observed.add(pair)
        pairs.append({"round": pair[0], "task": pair[1]})
    return {"source_run_id": source_run, "round_tasks": pairs}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nvst", description="Novita multi-model vendor tests")
    commands = parser.add_subparsers(dest="command", required=True)

    listing = commands.add_parser("list", help="list profile rows, suites, and selectors")
    listing.add_argument("--model", required=True)

    develop = commands.add_parser("develop", help="run ordered selectors and stop at first failure")
    develop.add_argument("--model", required=True)
    develop.add_argument("--service-access", required=True, type=Path)
    develop.add_argument("--judge-access", type=Path)
    develop.add_argument("--select", action="append", required=True)
    develop.add_argument("--include-long", action="append")
    develop.add_argument(
        "--acceptance",
        "--mode",
        dest="acceptance_mode",
        choices=("develop",),
        default="develop",
        help="profile acceptance mode (develop keeps explicit selectors)",
    )
    develop.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    develop.add_argument(
        "--concurrency",
        type=int,
        help="override supported runner concurrency for this run, including deepSWE",
    )
    develop.add_argument(
        "--deepswe-rounds",
        type=int,
        help="override the profile DeepSWE round count for this develop run",
    )
    develop.add_argument(
        "--deepswe-task",
        action="append",
        help="restrict a develop DeepSWE run to named task(s); repeat for multiple tasks",
    )
    develop.add_argument(
        "--deepswe-supplemental-source-run",
        help="source run id whose unresolved DeepSWE round/task pairs are being recovered",
    )
    develop.add_argument(
        "--deepswe-supplemental-pair",
        action="append",
        help="operator-authorized supplemental pair in ROUND:TASK form; repeat for each pair",
    )

    accept = commands.add_parser("accept", help="run a complete acceptance manifest")
    accept.add_argument("--model", required=True)
    accept.add_argument("--service-access", required=True, type=Path)
    accept.add_argument("--judge-access", type=Path)
    accept.add_argument("--include-long", action="append")
    accept.add_argument(
        "--acceptance",
        "--mode",
        dest="acceptance_mode",
        choices=("simple", "full", "precision-core", "k3-sop"),
        default="full",
        help="production acceptance scope, including the official K3 SOP pre-launch gate",
    )
    accept.add_argument("--concurrency", type=int, help="override supported runner concurrency for this acceptance run")
    accept.add_argument("--run-dir", type=Path, help=argparse.SUPPRESS)
    accept.add_argument(
        "--continue-on-failure",
        action="store_true",
        help="run every planned suite after ordinary failures; global service/safety stops still apply",
    )
    resume = commands.add_parser("resume", help="resume or re-report a run")
    resume.add_argument("--run", required=True, type=Path)
    resume.add_argument(
        "--continue-from",
        help=(
            "for a terminal acceptance run, archive and rerun starting at this suite_id "
            "or execution id; requires --continue-reason"
        ),
    )
    resume.add_argument(
        "--continue-reason",
        help="human-supplied reason required with --continue-from",
    )
    resume.add_argument(
        "--allow-repo-pin-mismatch",
        action="store_true",
        help=(
            "permit a terminal continuation after a tooling-only repository update; "
            "suite input hashes must still match exactly"
        ),
    )
    resume.add_argument(
        "--only-suite",
        action="store_true",
        help=(
            "with --continue-from, rerun only the named suite in place without "
            "archiving or touching later suites"
        ),
    )
    resume.add_argument(
        "--finalize-only",
        action="store_true",
        help=(
            "with --continue-from/--only-suite, only summarize existing execution "
            "records into a terminal suite result without new vendor traffic"
        ),
    )

    report = commands.add_parser("report", help="regenerate acceptance report")
    report.add_argument("--run", required=True, type=Path)

    doctor = commands.add_parser(
        "doctor", help="validate acceptance prerequisites without vendor traffic"
    )
    doctor.add_argument("--model", required=True)
    doctor.add_argument("--service-access", required=True, type=Path)
    doctor.add_argument("--judge-access", type=Path)
    doctor.add_argument("--online-canary", action="store_true")
    doctor.add_argument(
        "--acceptance",
        choices=("simple", "full", "precision-core", "k3-sop"),
        default="full",
        help="acceptance plan used for static execution-plan validation",
    )
    doctor.add_argument(
        "--capacity-probe",
        action="store_true",
        help="run explicit endpoint probes at concurrency 1, 10, and 20",
    )

    session = commands.add_parser("session", help="merge hash-compatible suite evidence")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_create = session_commands.add_parser("create")
    session_create.add_argument("--model", required=True)
    session_create.add_argument("--service-access", required=True, type=Path)
    session_create.add_argument("--judge-access", type=Path)
    session_create.add_argument("--run", action="append", required=True, type=Path)
    session_create.add_argument("--approval", action="append", type=Path)
    session_continue = session_commands.add_parser("continue")
    session_continue.add_argument("--session", required=True, type=Path)
    session_continue.add_argument("--run", action="append", required=True, type=Path)
    session_continue.add_argument("--approval", action="append", type=Path)
    session_report = session_commands.add_parser("report")
    session_report.add_argument("--session", required=True, type=Path)

    requirements = commands.add_parser("requirements", help="manage requirement snapshots")
    requirement_commands = requirements.add_subparsers(dest="requirements_command", required=True)
    refresh = requirement_commands.add_parser("refresh", help="diff a live document against snapshot")
    refresh.add_argument("--model", required=True)
    refresh.add_argument("--doc", required=True)
    refresh.add_argument("--stream-doc")
    refresh.add_argument("--accept-table-sha")
    refresh.add_argument("--apply", action="store_true")

    collect = commands.add_parser(
        "collect",
        help="index committed runs by served model and test time, and stage a package",
    )
    collect.add_argument("--model", help="restrict to one profile family, e.g. kimi-k3")
    collect.add_argument("--served-model", help="restrict to one observed served model")
    collect.add_argument("--since", help="only runs started at or after this UTC timestamp")
    collect.add_argument("--runs-root", type=Path, help="default: <repository>/runs")
    collect.add_argument(
        "--out",
        type=Path,
        help="output directory for index.json/index.md (default: <runs-root>/index)",
    )
    collect.add_argument(
        "--copy-bundles",
        action="store_true",
        help="stage each run's test-artifacts bundle under <served-model>/<time>-<run-id>/",
    )
    return parser


def _list(model_id: str) -> dict:
    profile = ModelProfile.load(model_id, allow_blocked=True)
    def required_artifacts(suite) -> list[str]:
        values = list(suite.completeness.get("required_artifacts") or [])
        values.extend(suite.completeness.get("required_artifact_patterns") or [])
        per_repeat = suite.completeness.get("per_repeat") or {}
        values.extend(per_repeat.get("required_artifacts") or [])
        values.extend(per_repeat.get("required_artifact_patterns") or [])
        return list(dict.fromkeys(str(value) for value in values))

    supplementary_rows = {row["row_id"] for _, row in profile.supplementary_rows()}
    return {
        "model": model_id,
        "source_status": profile.source["status"],
        "snapshot_revision": profile.source.get("snapshot_revision"),
        "official_rows": [row["row_id"] for _, row in profile.official_rows()],
        "supplementary_rows": sorted(supplementary_rows),
        "suites": [
            {
                "suite_id": suite.suite_id,
                "adapter": suite.adapter,
                "row_ids": list(suite.row_ids),
                "section": "supplementary"
                if any(row_id in supplementary_rows for row_id in suite.row_ids)
                else "official",
                "test_class": suite.test_class,
                "mandatory": suite.mandatory,
                "long_test": suite.long_test,
                "repeat": suite.config.get("repeat", 1),
                "selectors": sorted(suite.selectors),
                "entrypoint": (suite.config.get("runner_fingerprint") or {}).get("entrypoint"),
                "concurrency": {
                    key: value
                    for key, value in (suite.config.get("runner_fingerprint") or {}).items()
                    if key.startswith("concurrency") or key.endswith("_concurrency")
                },
                "round_scheduling": (suite.config.get("runner_fingerprint") or {}).get(
                    "round_scheduling"
                ),
                "retry_policy": suite.config.get("rerun_policy") or {},
                "required_artifacts": required_artifacts(suite),
            }
            for suite in profile.suites
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "list":
            output = _list(args.model)
            exit_code = 0
        elif args.command in {"develop", "accept"}:
            outcome = Orchestrator().execute(
                mode=args.command,
                model_id=args.model,
                service_access_path=args.service_access,
                judge_access_path=args.judge_access,
                selectors=getattr(args, "select", None),
                include_long=_long_values(args.include_long),
                concurrency_override=getattr(args, "concurrency", None),
                deepswe_rounds_override=getattr(args, "deepswe_rounds", None),
                deepswe_supplemental_plan=_deepswe_supplemental_plan(args),
                deepswe_tasks=getattr(args, "deepswe_task", None),
                acceptance_mode=args.acceptance_mode,
                continue_on_failure=getattr(args, "continue_on_failure", False),
                run_dir=args.run_dir,
            )
            output = {
                "run_dir": str(outcome.run_dir),
                "verdict": outcome.report["verdict"],
                "exit_code": outcome.exit_code,
            }
            exit_code = outcome.exit_code
        elif args.command == "resume":
            outcome = Orchestrator().resume(
                args.run,
                continue_from=args.continue_from,
                continue_reason=args.continue_reason,
                allow_repo_pin_mismatch=args.allow_repo_pin_mismatch,
                only_suite=args.only_suite,
                finalize_only=args.finalize_only,
            )
            output = {
                "run_dir": str(outcome.run_dir),
                "verdict": outcome.report["verdict"],
                "exit_code": outcome.exit_code,
            }
            exit_code = outcome.exit_code
        elif args.command == "report":
            output = regenerate_report(args.run)
            exit_code = 0
        elif args.command == "doctor":
            output = run_doctor(
                args.model,
                service_access_path=args.service_access,
                judge_access_path=args.judge_access,
                online_canary=args.online_canary,
                acceptance_mode=args.acceptance,
                capacity_probe=args.capacity_probe,
            )
            exit_code = 0 if output["status"] == "passed" else 2
        elif args.command == "session" and args.session_command == "create":
            output = create_session(
                args.model,
                service_access_path=args.service_access,
                judge_access_path=args.judge_access,
                runs=args.run,
                approval_paths=args.approval,
            )
            exit_code = 0
        elif args.command == "session" and args.session_command == "continue":
            output = continue_session(
                args.session,
                runs=args.run,
                approval_paths=args.approval,
            )
            exit_code = 0
        elif args.command == "session" and args.session_command == "report":
            output = report_session(args.session)
            exit_code = 0
        elif args.command == "requirements" and args.requirements_command == "refresh":
            output = refresh_requirements(
                args.model,
                doc=args.doc,
                stream_doc=args.stream_doc,
                accept_table_sha=args.accept_table_sha,
                apply=args.apply,
            )
            exit_code = 0
        elif args.command == "collect":
            repository_root = Path(__file__).resolve().parents[2]
            runs_root = args.runs_root or (repository_root / "runs")
            out_dir = args.out or (runs_root / "index")
            index = write_collection(
                runs_root,
                out_dir,
                model_family=args.model,
                served_model=args.served_model,
                since=args.since,
                copy_bundles=args.copy_bundles,
            )
            output = {
                "out_dir": str(out_dir.resolve()),
                "index_json": str((out_dir / "index.json").resolve()),
                "index_markdown": str((out_dir / "index.md").resolve()),
                "deployment_count": index["deployment_count"],
                "run_count": index["run_count"],
                "staged_bundle_count": len(index["staged_bundles"]),
                "deployments": [
                    {
                        "served_model": item["served_model"],
                        "run_count": item["run_count"],
                        "passed_rows": item["passed_rows"],
                        "failed_rows": item["failed_rows"],
                    }
                    for item in index["deployments"]
                ],
            }
            exit_code = 0
        else:
            raise ConfigError("command_invalid", "unsupported command")
    except UserInterrupted as exc:
        print(json.dumps({"error": "human_interrupted", "run_dir": str(exc.run_dir)}))
        return 3
    except ConfigError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

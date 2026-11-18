from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

from novita_self_test.cli import main as nvst_main
from novita_self_test.config import ConfigError, ModelProfile, ServiceAccess

AUTHORIZATION_RE = re.compile(r"(?i)^Authorization\s*:\s*Bearer\s+([^\s]+)$")
SECRET_ENV = "NVST_DYNAMIC_API_KEY"
JUDGE_SECRET_ENV = "PPIO_JUDGE_API_KEY"


def parse_authorization(value: str) -> str:
    match = AUTHORIZATION_RE.fullmatch(value.strip())
    if match is None:
        raise ConfigError(
            "authorization_header_invalid",
            "expected a complete Authorization: Bearer <token> header",
        )
    return match.group(1)


def parse_secret_input(value: str) -> tuple[str, str | None]:
    lines = value.splitlines()
    if not lines:
        raise ConfigError("authorization_header_invalid", "endpoint Authorization is required")
    endpoint = parse_authorization(lines[0])
    judge = parse_authorization(lines[1]) if len(lines) > 1 and lines[1].strip() else None
    if any(line.strip() for line in lines[2:]):
        raise ConfigError("authorization_header_invalid", "unexpected secret input fields")
    return endpoint, judge


def _headers(value: str) -> dict[str, str]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError("service_headers_invalid", f"invalid headers JSON: {exc}") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in parsed.items()
    ):
        raise ConfigError("service_headers_invalid", "headers JSON must be a string mapping")
    return parsed


def materialize_service_access(
    *,
    base_url: str,
    served_model: str,
    model_family: str,
    think_mode: str,
    headers_json: str,
    directory: Path,
) -> Path:
    headers = _headers(headers_json)
    identity = {
        "base_url": base_url.rstrip("/"),
        "served_model": served_model,
        "model_family": model_family,
        "think_mode": think_mode,
        "headers": headers,
    }
    digest = hashlib.sha256(
        json.dumps(identity, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    payload: dict[str, Any] = {
        **identity,
        "api_key_env": SECRET_ENV,
        "deployment_id": f"dynamic-{digest[:12]}",
        "runtime_id": "dynamic-endpoint",
        "source_targets": [],
    }
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{digest}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.chmod(0o600)
    temporary.replace(path)
    ServiceAccess.load(path)
    return path


def _selected_beam_suite(nvst_args: list[str]) -> Any | None:
    if not nvst_args or nvst_args[0] == "resume":
        return None
    try:
        model_id = nvst_args[nvst_args.index("--model") + 1]
    except (ValueError, IndexError) as exc:
        raise ConfigError("model_required", "dynamic command requires --model") from exc
    profile = ModelProfile.load(model_id)
    if nvst_args[0] in {"accept", "doctor"}:
        suites = profile.suites
    else:
        selectors = [
            nvst_args[index + 1]
            for index, token in enumerate(nvst_args[:-1])
            if token == "--select"
        ]
        suites = [suite for selector in selectors for suite, _ in profile.select([selector])]
    return next((suite for suite in suites if suite.adapter == "beam"), None)


def materialize_judge_access(*, suite: Any, directory: Path) -> Path:
    env = dict(suite.config.get("env") or {})
    fingerprint = dict(suite.config.get("runner_fingerprint") or {})
    payload = {
        "base_url": str(env["NVST_BEAM_JUDGE_BASE_URL"]).rstrip("/"),
        "model": str(env["NVST_BEAM_JUDGE_MODEL"]),
        "api_key_env": str(env["NVST_BEAM_JUDGE_API_KEY_ENV"]),
        "requests_per_minute": int(fingerprint["judge_requests_per_minute"]),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{digest}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run nvst with an ephemeral endpoint credential")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--served-model", required=True)
    parser.add_argument("--model-family", default="kimi-k3")
    parser.add_argument("--think-mode", default="opensource")
    parser.add_argument("--headers-json", default="{}")
    parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path.home() / ".config" / "novita-kimi-vendor" / "endpoints",
    )
    parser.add_argument(
        "--judge-config-dir",
        type=Path,
        default=Path.home() / ".config" / "novita-kimi-vendor" / "judges",
    )
    parser.add_argument("nvst_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    nvst_args = list(args.nvst_args)
    if nvst_args[:1] == ["--"]:
        nvst_args.pop(0)
    if not nvst_args or nvst_args[0] not in {"develop", "accept", "resume", "doctor"}:
        raise ConfigError(
            "dynamic_endpoint_command_invalid",
            "dynamic endpoint execution supports develop, accept, resume, or doctor",
        )
    secret_input = sys.stdin.read()
    endpoint_token, judge_token = parse_secret_input(secret_input)
    service_path = materialize_service_access(
        base_url=args.base_url,
        served_model=args.served_model,
        model_family=args.model_family,
        think_mode=args.think_mode,
        headers_json=args.headers_json,
        directory=args.config_dir,
    )
    os.environ[SECRET_ENV] = endpoint_token
    beam_suite = _selected_beam_suite(nvst_args)
    judge_path = None
    if beam_suite is not None:
        judge_path = materialize_judge_access(
            suite=beam_suite,
            directory=args.judge_config_dir,
        )
        if judge_token is not None:
            os.environ[JUDGE_SECRET_ENV] = judge_token
    elif judge_token is not None:
        os.environ[JUDGE_SECRET_ENV] = judge_token
    if nvst_args[0] in {"develop", "accept", "doctor"}:
        if "--service-access" in nvst_args:
            raise ConfigError(
                "service_access_forbidden",
                "dynamic endpoint execution owns --service-access",
            )
        nvst_args.extend(["--service-access", str(service_path)])
        if judge_path is not None:
            if "--judge-access" in nvst_args:
                raise ConfigError(
                    "judge_access_forbidden",
                    "dynamic endpoint execution owns --judge-access",
                )
            nvst_args.extend(["--judge-access", str(judge_path)])
    return nvst_main(nvst_args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as exc:
        print(json.dumps({"error": exc.code, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

AUTHORIZATION_RE = re.compile(r"(?i)^Authorization\s*:\s*Bearer\s+([^\s]+)$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
ROLES = {"endpoint", "judge"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_registry_path() -> Path:
    configured = os.environ.get("NVST_CREDENTIAL_REGISTRY")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".config" / "novita-kimi-vendor" / "credentials.json"
    )


def parse_authorization(value: str) -> str:
    match = AUTHORIZATION_RE.fullmatch(value.strip())
    if match is None:
        raise ValueError("expected a complete Authorization: Bearer <token> header")
    return f"Authorization: Bearer {match.group(1)}"


def fingerprint(authorization: str) -> str:
    return hashlib.sha256(authorization.encode("utf-8")).hexdigest()[:16]


def _empty_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": 0,
        "updated_at": utc_now(),
        "default_judge": None,
        "credentials": {},
    }


def _validate_registry(registry: dict[str, Any]) -> None:
    if registry.get("schema_version") != 1:
        raise ValueError("unsupported credential registry schema")
    credentials = registry.get("credentials")
    if not isinstance(credentials, dict):
        raise ValueError("credential registry credentials must be a mapping")
    for name, item in credentials.items():
        if not NAME_RE.fullmatch(name) or not isinstance(item, dict):
            raise ValueError(f"invalid credential entry: {name}")
        parse_authorization(str(item.get("authorization") or ""))
        roles = item.get("roles")
        if not isinstance(roles, list) or not roles or not set(roles) <= ROLES:
            raise ValueError(f"invalid roles for credential {name}")
        match = item.get("match")
        if not isinstance(match, dict):
            raise ValueError(f"invalid match contract for credential {name}")
        for key in ("exact_hosts", "host_suffixes", "path_prefixes"):
            values = match.get(key)
            if not isinstance(values, list) or not all(
                isinstance(value, str) and value for value in values
            ):
                raise ValueError(f"invalid {key} for credential {name}")
        observed = fingerprint(item["authorization"])
        if item.get("fingerprint") != observed:
            raise ValueError(f"fingerprint mismatch for credential {name}")
    default_judge = registry.get("default_judge")
    if default_judge is not None and (
        default_judge not in credentials
        or "judge" not in credentials[default_judge].get("roles", [])
    ):
        raise ValueError("default_judge must name a judge credential")


@contextmanager
def locked_registry(path: Path):
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            registry = _empty_registry()
            if path.is_file():
                if path.stat().st_mode & 0o077:
                    raise PermissionError(f"credential registry must be chmod 0600: {path}")
                registry = json.loads(path.read_text(encoding="utf-8"))
                _validate_registry(registry)
            yield registry
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def write_registry(path: Path, registry: dict[str, Any]) -> None:
    registry["revision"] = int(registry.get("revision") or 0) + 1
    registry["updated_at"] = utc_now()
    _validate_registry(registry)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".credentials.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(registry, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _normalized_hosts(values: list[str]) -> list[str]:
    return sorted({value.strip().lower().rstrip(".") for value in values if value.strip()})


def _normalized_suffixes(values: list[str]) -> list[str]:
    return sorted(
        {
            "." + value.strip().lower().lstrip(".").rstrip(".")
            for value in values
            if value.strip()
        }
    )


def _normalized_paths(values: list[str]) -> list[str]:
    return sorted(
        {
            value.strip() if value.strip().startswith("/") else "/" + value.strip()
            for value in values
            if value.strip()
        }
    )


def set_credential(args: argparse.Namespace) -> dict[str, Any]:
    if not NAME_RE.fullmatch(args.name):
        raise ValueError("credential name must match [a-z][a-z0-9-]{1,63}")
    roles = sorted(set(args.role or []))
    if not roles or not set(roles) <= ROLES:
        raise ValueError("at least one valid --role is required")
    exact_hosts = _normalized_hosts(args.exact_host or [])
    suffixes = _normalized_suffixes(args.host_suffix or [])
    paths = _normalized_paths(args.path_prefix or [])
    if not exact_hosts and not suffixes:
        raise ValueError("at least one --exact-host or --host-suffix is required")
    authorization = parse_authorization(sys.stdin.read())
    with locked_registry(args.registry) as registry:
        registry["credentials"][args.name] = {
            "authorization": authorization,
            "fingerprint": fingerprint(authorization),
            "roles": roles,
            "match": {
                "exact_hosts": exact_hosts,
                "host_suffixes": suffixes,
                "path_prefixes": paths,
            },
            "updated_at": utc_now(),
        }
        if args.default_judge:
            if "judge" not in roles:
                raise ValueError("--default-judge requires --role judge")
            registry["default_judge"] = args.name
        write_registry(args.registry, registry)
        return public_entry(args.name, registry["credentials"][args.name])


def remove_credential(args: argparse.Namespace) -> dict[str, Any]:
    with locked_registry(args.registry) as registry:
        if args.name not in registry["credentials"]:
            raise KeyError(f"credential not found: {args.name}")
        del registry["credentials"][args.name]
        if registry.get("default_judge") == args.name:
            registry["default_judge"] = None
        write_registry(args.registry, registry)
        return {"removed": args.name}


def _match_score(item: dict[str, Any], url: str, role: str) -> tuple[int, int] | None:
    if role not in item["roles"]:
        return None
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path or "/"
    match = item["match"]
    exact = host in match["exact_hosts"]
    suffix_lengths = [
        len(suffix)
        for suffix in match["host_suffixes"]
        if host.endswith(suffix) and host != suffix.lstrip(".")
    ]
    if not exact and not suffix_lengths:
        return None
    path_lengths = [len(prefix) for prefix in match["path_prefixes"] if path.startswith(prefix)]
    if match["path_prefixes"] and not path_lengths:
        return None
    return (10000 if exact else max(suffix_lengths), max(path_lengths, default=0))


def resolve_credential(
    registry: dict[str, Any], *, url: str | None, role: str, name: str | None
) -> tuple[str, dict[str, Any]]:
    if role not in ROLES:
        raise ValueError(f"invalid credential role: {role}")
    if name:
        item = registry["credentials"].get(name)
        if item is None or role not in item["roles"]:
            raise KeyError(f"credential {name!r} does not provide role {role!r}")
        return name, item
    if role == "judge" and not url and registry.get("default_judge"):
        selected = registry["default_judge"]
        return selected, registry["credentials"][selected]
    if not url:
        raise ValueError("--url or --name is required")
    candidates = []
    for candidate_name, item in registry["credentials"].items():
        score = _match_score(item, url, role)
        if score is not None:
            candidates.append((score, candidate_name, item))
    if not candidates:
        raise KeyError(f"no {role} credential matches URL")
    candidates.sort(reverse=True, key=lambda candidate: candidate[0])
    best_score = candidates[0][0]
    best = [candidate for candidate in candidates if candidate[0] == best_score]
    if len(best) != 1:
        raise ValueError(f"ambiguous {role} credential match: {[item[1] for item in best]}")
    _, selected_name, selected_item = best[0]
    return selected_name, selected_item


def resolve(args: argparse.Namespace) -> int:
    with locked_registry(args.registry) as registry:
        _, item = resolve_credential(
            registry,
            url=args.url,
            role=args.role,
            name=args.name,
        )
        sys.stdout.write(item["authorization"])
        sys.stdout.flush()
    return 0


def public_entry(name: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "fingerprint": item["fingerprint"],
        "roles": item["roles"],
        "match": item["match"],
        "updated_at": item["updated_at"],
    }


def list_registry(args: argparse.Namespace) -> dict[str, Any]:
    with locked_registry(args.registry) as registry:
        return {
            "schema_version": registry["schema_version"],
            "revision": registry["revision"],
            "path": str(args.registry),
            "mode": oct(args.registry.stat().st_mode & 0o777) if args.registry.exists() else None,
            "default_judge": registry.get("default_judge"),
            "credentials": [
                public_entry(name, item)
                for name, item in sorted(registry["credentials"].items())
            ],
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage private nvst endpoint credentials")
    parser.add_argument("--registry", type=Path, default=default_registry_path())
    commands = parser.add_subparsers(dest="command", required=True)
    set_cmd = commands.add_parser("set", help="read Authorization from stdin and upsert entry")
    set_cmd.add_argument("--name", required=True)
    set_cmd.add_argument("--role", action="append", choices=sorted(ROLES), required=True)
    set_cmd.add_argument("--exact-host", action="append")
    set_cmd.add_argument("--host-suffix", action="append")
    set_cmd.add_argument("--path-prefix", action="append")
    set_cmd.add_argument("--default-judge", action="store_true")
    remove = commands.add_parser("remove")
    remove.add_argument("--name", required=True)
    resolve_cmd = commands.add_parser("resolve")
    resolve_cmd.add_argument("--url")
    resolve_cmd.add_argument("--role", choices=sorted(ROLES), required=True)
    resolve_cmd.add_argument("--name")
    commands.add_parser("list")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.registry = args.registry.expanduser().resolve()
    if args.command == "set":
        output = set_credential(args)
    elif args.command == "remove":
        output = remove_credential(args)
    elif args.command == "resolve":
        return resolve(args)
    else:
        output = list_registry(args)
    print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from exc

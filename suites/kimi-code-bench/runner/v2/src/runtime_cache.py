"""Prepare and validate the shared, pin-keyed benchmark runtime cache."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator


CACHE_SCHEMA_VERSION = 1
KIMI_REPOSITORY = "https://github.com/MoonshotAI/kimi-cli"
TERMINAL_BENCH_REPOSITORY = "https://github.com/laude-institute/terminal-bench-2"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _proxy_environment(proxy: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update({
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    })
    return environment


def _run(
    command: list[str],
    *,
    environment: dict[str, str],
    cwd: Path | None = None,
    timeout: int = 900,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    print("runtime-cache:", " ".join(command), flush=True)
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        timeout=timeout,
        check=True,
        text=True,
        capture_output=capture_output,
    )


def _output(command: list[str], *, environment: dict[str, str], cwd: Path | None = None) -> str:
    return _run(command, environment=environment, cwd=cwd, capture_output=True).stdout.strip()


@contextmanager
def _cache_lock(cache_root: Path, name: str, timeout: int = 900) -> Iterator[None]:
    lock_root = cache_root / "locks"
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / f"{name}.lock"
    with lock_path.open("a+") as handle:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for runtime cache lock: {name}")
                time.sleep(0.2)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _quarantine(target: Path) -> Path:
    suffix = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = target.with_name(f"{target.name}.invalid-{suffix}-{os.getpid()}")
    target.rename(destination)
    return destination


def _ensure_cached_directory(
    cache_root: Path,
    target: Path,
    lock_name: str,
    valid: Callable[[Path], bool],
    build: Callable[[Path], None],
) -> bool:
    if valid(target):
        return True
    with _cache_lock(cache_root, lock_name):
        if valid(target):
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            quarantined = _quarantine(target)
            print(f"runtime-cache: quarantined invalid cache at {quarantined}", flush=True)
        staging_root = cache_root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f"{lock_name}-", dir=staging_root))
        try:
            build(stage)
            if not valid(stage):
                raise RuntimeError(f"new runtime cache failed validation: {lock_name}")
            stage.replace(target)
        finally:
            if stage.exists():
                shutil.rmtree(stage)
    return False


def _source_manifest_path(root: Path) -> Path:
    return root / ".git/kbench-cache-manifest.json"


def _source_valid(root: Path, repository: str, revision: str, environment: dict[str, str]) -> bool:
    manifest = _read_json(_source_manifest_path(root))
    if manifest != {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "kind": "git_source",
        "repository": repository,
        "revision": revision,
    }:
        return False
    try:
        head = _output(["git", "-C", str(root), "rev-parse", "HEAD"], environment=environment)
        dirty = _output(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all"],
            environment=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return head == revision and not dirty


def _build_source(
    stage: Path,
    repository: str,
    revision: str,
    environment: dict[str, str],
    seed: Path | None,
) -> None:
    if seed is not None and not (seed / ".git").exists():
        seed = None
    if seed is not None and (seed / ".git").exists():
        try:
            seed_head = _output(["git", "-C", str(seed), "rev-parse", "HEAD"], environment=environment)
        except (OSError, subprocess.SubprocessError):
            seed_head = ""
        if seed_head == revision:
            _run(
                ["git", "clone", "--local", "--no-hardlinks", str(seed), str(stage)],
                environment=environment,
            )
        else:
            seed = None
    if seed is None:
        _run(["git", "init", str(stage)], environment=environment)
        _run(["git", "-C", str(stage), "remote", "add", "origin", repository], environment=environment)
        _run(
            ["git", "-C", str(stage), "fetch", "--depth", "1", "origin", revision],
            environment=environment,
        )
    _run(["git", "-C", str(stage), "checkout", "--detach", revision], environment=environment)
    _run(["git", "-C", str(stage), "clean", "-ffd"], environment=environment)
    _source_manifest_path(stage).write_text(json.dumps({
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "kind": "git_source",
        "repository": repository,
        "revision": revision,
    }, indent=2) + "\n")


def _ensure_source(
    cache_root: Path,
    name: str,
    repository: str,
    revision: str,
    environment: dict[str, str],
    seed: Path | None,
) -> tuple[Path, bool]:
    target = cache_root / "sources" / name / revision
    hit = _ensure_cached_directory(
        cache_root,
        target,
        f"source-{name}-{revision}",
        lambda path: _source_valid(path, repository, revision, environment),
        lambda stage: _build_source(stage, repository, revision, environment, seed),
    )
    return target, hit


def _harbor_valid(root: Path, version: str, python_spec: str, environment: dict[str, str]) -> bool:
    manifest = _read_json(root / "kbench-cache-manifest.json")
    if (
        manifest.get("cache_schema_version") != CACHE_SCHEMA_VERSION
        or manifest.get("kind") != "harbor_venv"
        or manifest.get("harbor_version") != version
        or manifest.get("python_spec") != python_spec
    ):
        return False
    try:
        harbor_output = _output([str(root / "bin/harbor"), "--version"], environment=environment)
        python_output = _output([str(root / "bin/python"), "--version"], environment=environment)
    except (OSError, subprocess.SubprocessError):
        return False
    return version in harbor_output and re.search(rf"Python {re.escape(python_spec)}(?:\.|$)", python_output) is not None


def _build_harbor(
    stage: Path,
    uv_path: Path,
    version: str,
    python_spec: str,
    environment: dict[str, str],
) -> None:
    _run(
        [
            str(uv_path), "venv", "--no-project", "--relocatable",
            "--python", python_spec, str(stage),
        ],
        environment=environment,
    )
    _run(
        [
            str(uv_path), "pip", "install", "--strict", "--python", str(stage / "bin/python"),
            f"harbor=={version}",
        ],
        environment=environment,
    )
    harbor_output = _output([str(stage / "bin/harbor"), "--version"], environment=environment)
    python_output = _output([str(stage / "bin/python"), "--version"], environment=environment)
    (stage / "kbench-cache-manifest.json").write_text(json.dumps({
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "kind": "harbor_venv",
        "harbor_version": version,
        "python_spec": python_spec,
        "harbor_version_output": harbor_output,
        "python_version_output": python_output,
    }, indent=2) + "\n")


def _ensure_harbor(
    cache_root: Path,
    uv_path: Path,
    version: str,
    python_spec: str,
    environment: dict[str, str],
) -> tuple[Path, bool]:
    target = cache_root / "tools" / "harbor" / f"{version}-python{python_spec}"
    hit = _ensure_cached_directory(
        cache_root,
        target,
        f"harbor-{version}-python{python_spec}",
        lambda path: _harbor_valid(path, version, python_spec, environment),
        lambda stage: _build_harbor(stage, uv_path, version, python_spec, environment),
    )
    return target, hit


def _wheel_valid(root: Path, revision: str) -> bool:
    manifest = _read_json(root / "kbench-cache-manifest.json")
    relative = manifest.get("wheel")
    wheel = root / relative if isinstance(relative, str) else root / "missing"
    return (
        manifest.get("cache_schema_version") == CACHE_SCHEMA_VERSION
        and manifest.get("kind") == "kimi_cli_wheel"
        and manifest.get("revision") == revision
        and wheel.is_file()
        and manifest.get("sha256") == _sha256(wheel)
    )


def _build_wheel(
    stage: Path,
    uv_path: Path,
    source: Path,
    revision: str,
    environment: dict[str, str],
) -> None:
    output = stage / "dist"
    output.mkdir()
    _run(
        [str(uv_path), "build", "--package", "kimi-cli", "--out-dir", str(output)],
        environment=environment,
        cwd=source,
        timeout=600,
    )
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one Kimi CLI wheel, found {len(wheels)}")
    wheel = wheels[0]
    (stage / "kbench-cache-manifest.json").write_text(json.dumps({
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "kind": "kimi_cli_wheel",
        "revision": revision,
        "wheel": str(wheel.relative_to(stage)),
        "sha256": _sha256(wheel),
    }, indent=2) + "\n")


def _ensure_wheel(
    cache_root: Path,
    uv_path: Path,
    source: Path,
    revision: str,
    environment: dict[str, str],
) -> tuple[Path, bool]:
    target = cache_root / "wheels" / "kimi-cli" / revision
    hit = _ensure_cached_directory(
        cache_root,
        target,
        f"wheel-kimi-cli-{revision}",
        lambda path: _wheel_valid(path, revision),
        lambda stage: _build_wheel(stage, uv_path, source, revision, environment),
    )
    manifest = _read_json(target / "kbench-cache-manifest.json")
    return target / manifest["wheel"], hit


def prepare_runtime(
    *,
    cache_root: Path,
    uv_path: Path,
    uv_version: str,
    harbor_version: str,
    harbor_python: str,
    kimi_ref: str,
    terminal_ref: str,
    proxy: str,
    output_path: Path,
    kimi_seed: Path | None = None,
    terminal_seed: Path | None = None,
) -> dict:
    environment = _proxy_environment(proxy)
    uv_output = _output([str(uv_path), "--version"], environment=environment)
    if uv_output != f"uv {uv_version} (x86_64-unknown-linux-gnu)" and not uv_output.startswith(f"uv {uv_version} "):
        raise RuntimeError(f"uv version mismatch: {uv_output}")
    cache_root.mkdir(parents=True, exist_ok=True)
    kimi_source, kimi_source_hit = _ensure_source(
        cache_root, "kimi-cli", KIMI_REPOSITORY, kimi_ref, environment, kimi_seed
    )
    terminal_source, terminal_source_hit = _ensure_source(
        cache_root, "terminal-bench-2", TERMINAL_BENCH_REPOSITORY, terminal_ref, environment, terminal_seed
    )
    wheel, wheel_hit = _ensure_wheel(cache_root, uv_path, kimi_source, kimi_ref, environment)
    harbor, harbor_hit = _ensure_harbor(
        cache_root, uv_path, harbor_version, harbor_python, environment
    )
    harbor_output = _output([str(harbor / "bin/harbor"), "--version"], environment=environment)
    python_output = _output([str(harbor / "bin/python"), "--version"], environment=environment)
    result = {
        "cache_schema_version": CACHE_SCHEMA_VERSION,
        "prepared_at": _now(),
        "cache_root": str(cache_root),
        "cache_hits": {
            "kimi_cli_source": kimi_source_hit,
            "terminal_bench_2_source": terminal_source_hit,
            "kimi_cli_wheel": wheel_hit,
            "harbor": harbor_hit,
        },
        "uv_version_output": uv_output,
        "harbor_version_output": harbor_output,
        "harbor_python_output": python_output,
        "kimi_cli_ref": kimi_ref,
        "terminal_bench_2_ref": terminal_ref,
        "kimi_cli_wheel_sha256": _sha256(wheel),
        "paths": {
            "harbor": str(harbor / "bin/harbor"),
            "kimi_cli_source": str(kimi_source),
            "terminal_bench_2_source": str(terminal_source),
            "kimi_cli_wheel": str(wheel),
        },
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(output_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument("--uv-path", required=True, type=Path)
    parser.add_argument("--uv-version", required=True)
    parser.add_argument("--harbor-version", required=True)
    parser.add_argument("--harbor-python", required=True)
    parser.add_argument("--kimi-ref", required=True)
    parser.add_argument("--terminal-ref", required=True)
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--kimi-seed", type=Path)
    parser.add_argument("--terminal-seed", type=Path)
    args = parser.parse_args()
    prepare_runtime(
        cache_root=args.cache_root,
        uv_path=args.uv_path,
        uv_version=args.uv_version,
        harbor_version=args.harbor_version,
        harbor_python=args.harbor_python,
        kimi_ref=args.kimi_ref,
        terminal_ref=args.terminal_ref,
        proxy=args.proxy,
        output_path=args.output,
        kimi_seed=args.kimi_seed,
        terminal_seed=args.terminal_seed,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

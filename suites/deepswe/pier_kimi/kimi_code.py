"""Pier agent adapter for the Kimi Code CLI (``@moonshot-ai/kimi-code``).

Registers ``kimi-code`` as a Pier installed agent so DeepSWE can be run against
a self-hosted Kimi K3 endpoint:

    pier run -p deep-swe/tasks \
        --agent-import-path pier_kimi.kimi_code:KimiCode \
        -m kimi/kimi-k3 --ak version=0.29.0 \
        --ae KIMI_BASE_URL=https://<endpoint>/v1

The API key is inherited from the Pier host environment so it never appears in
the Pier process command line.

The runtime config mirrors the parameters the K3 serving requirements doc
pins for the official DeepSWE numbers (avg@3 = 66.7):

    [loop_control] max_steps_per_turn = 0
    [background]   print_background_mode = "steer"
                   print_wait_ceiling_s = 315360000
                   print_max_turns      = 100000
    [subagent]     timeout_ms = 259200000

plus ``thinking.effort = "max"``, ``thinking.keep = "all"`` and a
1048576-token context window.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from pier.agents.installed.base import (
    BaseInstalledAgent,
    CliFlag,
    with_prompt_template,
)
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext
from pier.models.agent.install import AgentInstallSpec, InstallStep
from pier.models.agent.network import NetworkAllowlist
from pier.models.trial.paths import EnvironmentPaths

from pier_kimi.trajectory import build_trajectory

# Kimi Code writes config + sessions under KIMI_CODE_HOME. Keep it inside
# /logs/agent so the wire logs land in the trial directory on the host.
KIMI_CODE_HOME = EnvironmentPaths.agent_dir / "kimi-home"
NPM_REGISTRY_HOST = "registry.npmjs.org"
NODE_VERSION = "22.14.0"
# Kimi Code requires a non-empty key even though the local proxy owns auth.
PLACEHOLDER_API_KEY = "not-used-by-endpoint"
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 18080
PROXY_BASE_URL = f"http://{PROXY_HOST}:{PROXY_PORT}/v1"
DEFAULT_CONTEXT_SIZE = 1048576
PROVIDER_ALIAS = "selfhosted"


class KimiCode(BaseInstalledAgent):
    """Drive the Kimi Code CLI in ``kimi -p`` (non-interactive) mode."""

    SUPPORTS_ATIF: bool = True

    _OUTPUT_FILENAME = "kimi-code.jsonl"
    _STDERR_FILENAME = "kimi-code.stderr.txt"

    CLI_FLAGS = [
        CliFlag("model_alias", cli="-m", type="str"),
    ]

    def __init__(
        self,
        logs_dir: Path,
        thinking_effort: str = "max",
        thinking_keep: str = "all",
        max_context_size: int = DEFAULT_CONTEXT_SIZE,
        provider_type: str = "kimi",
        max_steps_per_turn: int = 0,
        print_background_mode: str = "steer",
        print_wait_ceiling_s: int = 315360000,
        print_max_turns: int = 100000,
        subagent_timeout_ms: int = 259200000,
        extra_config: dict[str, Any] | None = None,
        *args,
        **kwargs,
    ):
        self._thinking_effort = thinking_effort
        self._thinking_keep = thinking_keep
        self._max_context_size = int(max_context_size)
        self._provider_type = provider_type
        self._max_steps_per_turn = int(max_steps_per_turn)
        self._print_background_mode = print_background_mode
        self._print_wait_ceiling_s = int(print_wait_ceiling_s)
        self._print_max_turns = int(print_max_turns)
        self._subagent_timeout_ms = int(subagent_timeout_ms)
        self._extra_config = extra_config or {}
        super().__init__(logs_dir, *args, **kwargs)

    @staticmethod
    def name() -> str:
        return "kimi-code"

    # ---------------------------------------------------------------- identity

    @property
    def _served_model(self) -> str:
        """The model id sent to the API (``kimi/kimi-k3`` -> ``kimi-k3``)."""
        if not self.model_name:
            raise ValueError(
                "KimiCode requires a model name, e.g. -m kimi/kimi-k3 or -m kimi-k3"
            )
        return self.model_name.split("/", 1)[-1]

    @property
    def _model_alias(self) -> str:
        """The alias registered in config.toml and passed to ``kimi -m``."""
        return f"{PROVIDER_ALIAS}/{self._served_model}"

    def _endpoint_base_url(self) -> str:
        base_url = (
            self._get_env("KIMI_BASE_URL")
            or self._get_env("KIMI_MODEL_BASE_URL")
            or os.environ.get("KIMI_BASE_URL")
            or os.environ.get("KIMI_MODEL_BASE_URL")
        )
        if not base_url:
            raise ValueError(
                "KimiCode requires KIMI_BASE_URL (agent.env or --ae), "
                "e.g. https://your-endpoint/v1"
            )
        return base_url.rstrip("/")

    def _base_url(self) -> str:
        """Use the per-trial proxy as the only URL written to Kimi config."""
        return PROXY_BASE_URL

    # ----------------------------------------------------------------- install

    def get_version_command(self) -> str | None:
        return 'export PATH="$HOME/.local/bin:$PATH"; kimi --version'

    def parse_version(self, stdout: str) -> str:
        import re

        match = re.search(r"(\d+\.\d+\.\d+)", stdout.strip())
        return match.group(1) if match else stdout.strip()

    def install_spec(self) -> AgentInstallSpec:
        package_version = f"@{self._version}" if self._version else "@latest"
        # DeepSWE's own images all derive from `mars-base`, which already ships
        # Node 24 / npm 11 / curl / tar — there this step is a no-op. It exists
        # for leaner images (e.g. pier's own bare `ubuntu:24.04` examples) that
        # have neither node nor curl, leaving the agent step unable to even
        # fetch Node. Guarded on curl+tar being absent so that no package
        # manager runs on the DeepSWE path, where the egress allowlist opens
        # only the npm registry and the model endpoint, not distro mirrors.
        root_run = (
            "if command -v curl > /dev/null 2>&1 && command -v tar > /dev/null 2>&1; then"
            '  echo "curl and tar present; skipping package install";'
            " elif command -v apt-get > /dev/null 2>&1; then"
            "  apt-get update && apt-get install -y --no-install-recommends"
            "    curl ca-certificates tar xz-utils;"
            " elif command -v apk > /dev/null 2>&1; then"
            "  apk add --no-cache curl ca-certificates tar;"
            " elif command -v yum > /dev/null 2>&1; then"
            "  yum install -y curl ca-certificates tar;"
            " else"
            '  echo "No package manager and no curl/tar; agent install will fail" >&2;'
            "  exit 1;"
            " fi"
        )
        # Kimi Code needs Node >= 20. DeepSWE task images are language-specific
        # (go / python / rust / ts bases), so most either lack node entirely or
        # ship an older distro build. Install a pinned official tarball into
        # ~/.local whenever the available node is too old. .tar.gz is used over
        # .tar.xz so no xz-utils dependency is required.
        agent_run = (
            "set -euo pipefail; "
            'export NPM_CONFIG_PREFIX="$HOME/.local"; '
            'export PATH="$HOME/.local/bin:$PATH"; '
            # `|| true` matters: under `set -e` a missing node would abort the
            # whole install at this assignment, never reaching the fallback.
            "node_major=$(node -v 2>/dev/null | sed 's/^v//' | cut -d. -f1 || true); "
            'case "$node_major" in ""|*[!0-9]*) node_major=0 ;; esac; '
            'if [ "$node_major" -lt 20 ]; then '
            "  case \"$(uname -m)\" in"
            "    x86_64|amd64) node_arch=x64 ;;"
            "    aarch64|arm64) node_arch=arm64 ;;"
            '    *) echo "Unsupported arch for node install: $(uname -m)" >&2; exit 1 ;;'
            "  esac; "
            f'  node_tar="node-v{NODE_VERSION}-linux-$node_arch.tar.gz"; '
            f'  curl -fsSL "https://nodejs.org/dist/v{NODE_VERSION}/$node_tar"'
            '    -o /tmp/node.tar.gz && '
            '  mkdir -p "$HOME/.local" && '
            '  tar -xzf /tmp/node.tar.gz -C "$HOME/.local" --strip-components=1 && '
            "  rm -f /tmp/node.tar.gz; "
            "fi && "
            "node -v && npm -v && "
            f"npm install -g @moonshot-ai/kimi-code{package_version} && "
            "printf '%s\\n' 'export NPM_CONFIG_PREFIX=\"$HOME/.local\"' "
            "'export PATH=\"$HOME/.local/bin:$PATH\"' >> ~/.bashrc && "
            "kimi --version"
        )
        return AgentInstallSpec(
            agent_name=self.name(),
            version=self._version,
            steps=[
                InstallStep(
                    user="root",
                    env={"DEBIAN_FRONTEND": "noninteractive"},
                    run=root_run,
                ),
                InstallStep(user="agent", run=agent_run),
            ],
            verification_command=self.get_version_command(),
        )

    def network_allowlist(self) -> NetworkAllowlist:
        """Allow the model endpoint plus whatever install needs."""
        domains = {NPM_REGISTRY_HOST, "registry.yarnpkg.com", "nodejs.org"}
        parsed = urlparse(self._endpoint_base_url())
        if parsed.hostname:
            domains.add(parsed.hostname.lower())
        for key in ("KIMI_SEARCH_BASE_URL", "KIMI_FETCH_BASE_URL"):
            if value := self._get_env(key):
                host = urlparse(value).hostname
                if host:
                    domains.add(host.lower())
        return NetworkAllowlist(domains=sorted(domains))

    # ------------------------------------------------------------------ config

    def _build_config(self) -> dict[str, Any]:
        """The ``config.toml`` contents, as a plain dict."""
        alias = self._model_alias
        config: dict[str, Any] = {
            "default_model": alias,
            "default_permission_mode": "auto",
            "default_plan_mode": False,
            "telemetry": False,
            "providers": {
                PROVIDER_ALIAS: {
                    "type": self._provider_type,
                    "base_url": self._base_url(),
                    # Must be non-empty: KimiChatProvider hard-fails with
                    # "apiKey is required" on an empty string, even when the
                    # endpoint itself performs no authentication. Verified
                    # against kimi-code 0.29.0.
                    "api_key": PLACEHOLDER_API_KEY,
                }
            },
            "models": {
                alias: {
                    "provider": PROVIDER_ALIAS,
                    "model": self._served_model,
                    "max_context_size": self._max_context_size,
                    "capabilities": [
                        "thinking",
                        "always_thinking",
                        "image_in",
                        "tool_use",
                    ],
                    "display_name": self._served_model,
                    "support_efforts": ["low", "high", "max"],
                    "default_effort": self._thinking_effort,
                }
            },
            "thinking": {
                "enabled": True,
                "effort": self._thinking_effort,
                "keep": self._thinking_keep,
            },
            "loop_control": {
                "max_steps_per_turn": self._max_steps_per_turn,
            },
            "background": {
                "print_background_mode": self._print_background_mode,
                "print_wait_ceiling_s": self._print_wait_ceiling_s,
                "print_max_turns": self._print_max_turns,
            },
            "subagent": {
                "timeout_ms": self._subagent_timeout_ms,
            },
        }
        _deep_merge(config, self._extra_config)
        return config

    def _write_config_command(self) -> str:
        """Shell that materialises config.toml inside the sandbox."""
        toml_text = _to_toml(self._build_config())
        home = shlex.quote(str(KIMI_CODE_HOME))
        # Quoted heredoc: no shell expansion inside the config body, so API
        # keys or URLs containing $ / backticks survive verbatim.
        return (
            f"mkdir -p {home} && "
            f"cat > {home}/config.toml << 'KIMI_CONFIG_EOF'\n"
            f"{toml_text}"
            "KIMI_CONFIG_EOF\n"
        )

    # --------------------------------------------------------------------- run

    @with_prompt_template
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        escaped_instruction = shlex.quote(instruction)

        # Pier does not implicitly forward the host environment into the
        # Harbor agent container. The proxy is the only component that needs
        # the real credential; pass it through the ephemeral agent env and
        # remove it before launching Kimi CLI.
        api_key = self._get_env("KIMI_API_KEY") or os.environ.get("KIMI_API_KEY")
        if not api_key:
            raise RuntimeError("KimiCode requires KIMI_API_KEY for the request proxy")

        env = self.build_process_env(
            {
                "KIMI_CODE_HOME": str(KIMI_CODE_HOME),
                "KIMI_DISABLE_TELEMETRY": "1",
                "KIMI_CODE_NO_AUTO_UPDATE": "1",
                # Config file is authoritative; these mirror it so that env
                # overrides stay consistent if the CLI prefers them.
                "KIMI_LOOP_MAX_STEPS_PER_TURN": str(self._max_steps_per_turn),
                "KIMI_SUBAGENT_TIMEOUT_MS": str(self._subagent_timeout_ms),
                "KIMI_MODEL_THINKING_EFFORT": self._thinking_effort,
                "KIMI_MODEL_THINKING_KEEP": self._thinking_keep,
                "KIMI_PROXY_TARGET": self._endpoint_base_url(),
            }
        )

        await self.exec_as_agent(
            environment, command=self._write_config_command(), env=env
        )

        # The Pier agent package is imported on the host, but Harbor task
        # containers do not inherit the host PYTHONPATH. Upload the small
        # proxy package into the task container before starting it.
        package_target = "/tmp/pier_kimi/pier_kimi"
        await environment.exec(
            f"rm -rf {shlex.quote(package_target)} && mkdir -p {shlex.quote(package_target)}"
        )
        await environment.upload_dir(Path(__file__).resolve().parent, package_target)

        # Upload the credential through the container file API instead of
        # putting it in `docker compose exec -e`, whose process listing would
        # expose the token. The proxy removes this file on exit.
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
            handle.write(api_key)
            host_key_path = Path(handle.name)
        try:
            host_key_path.chmod(0o600)
            await environment.upload_file(host_key_path, "/tmp/kimi-api-key")
        finally:
            host_key_path.unlink(missing_ok=True)

        output_path = EnvironmentPaths.agent_dir / self._OUTPUT_FILENAME
        stderr_path = EnvironmentPaths.agent_dir / self._STDERR_FILENAME
        # stdout and stderr are kept separate on purpose: in stream-json mode
        # only stdout is JSONL. Thinking content, tool progress and resume hints
        # go to stderr, so merging them would corrupt the JSONL stream.
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                'export PATH="$HOME/.local/bin:$PATH"; '
                'export PYTHONPATH="/tmp/pier_kimi${PYTHONPATH:+:$PYTHONPATH}"; '
                "proxy_key=/tmp/kimi-api-key; "
                "proxy_log=/logs/agent/request-proxy.log; "
                "proxy_evidence=/logs/agent/request-evidence; "
                "test -s \"$proxy_key\"; "
                "python3 -m pier_kimi.request_proxy "
                "--target \"$KIMI_PROXY_TARGET\" "
                "--api-key-file \"$proxy_key\" "
                "--output-dir \"$proxy_evidence\" "
                "--extra-headers-json \"${KIMI_EXTRA_HEADERS_JSON:-{}}\" "
                f">\"$proxy_log\" 2>&1 & proxy_pid=$!; "
                "cleanup_proxy() { kill \"$proxy_pid\" 2>/dev/null || true; "
                "wait \"$proxy_pid\" 2>/dev/null || true; rm -f \"$proxy_key\"; }; "
                "trap cleanup_proxy EXIT; "
                "proxy_ready=0; "
                "for _ in $(seq 1 100); do "
                f"  if curl -fsS http://{PROXY_HOST}:{PROXY_PORT}/__health >/dev/null; then proxy_ready=1; break; fi; "
                "  sleep 0.1; "
                "done; "
                'test "$proxy_ready" = 1; '
                # Keep the real key in the proxy process only; Kimi Code must
                # use the placeholder from config.toml and cannot log it.
                "unset KIMI_API_KEY; "
                f"kimi -p {escaped_instruction} "
                f"-m {shlex.quote(self._model_alias)} "
                "--output-format stream-json "
                f"2>{stderr_path} "
                f"</dev/null | stdbuf -oL tee {output_path}"
            ),
            env=env,
        )

    # ---------------------------------------------------------- post-processing

    @property
    def _wire_log_glob(self) -> str:
        return "kimi-home/sessions/*/*/agents/*/wire.jsonl"

    def populate_context_post_run(self, context: AgentContext) -> None:
        """Parse stream-json + wire logs into an ATIF trajectory and metrics."""
        stdout_path = self.logs_dir / self._OUTPUT_FILENAME
        wire_paths = sorted(self.logs_dir.glob(self._wire_log_glob))

        try:
            trajectory = build_trajectory(
                agent_name=self.name(),
                agent_version=self.version() or "unknown",
                model_name=self.model_name,
                reasoning_effort=self._thinking_effort,
                stdout_path=stdout_path,
                wire_paths=wire_paths,
            )
        except Exception as exc:  # trajectory parsing must never fail a trial
            self.logger.warning(f"Failed to build Kimi Code trajectory: {exc}")
            return

        if trajectory is None:
            self.logger.warning("No Kimi Code trajectory could be reconstructed")
            return

        trajectory_path = self.logs_dir / "trajectory.json"
        trajectory_path.write_text(
            json.dumps(trajectory.model_dump(exclude_none=True), indent=2)
        )

        if trajectory.final_metrics is not None:
            from pier.utils.trajectory_metrics import (
                populate_context_from_final_metrics,
            )

            populate_context_from_final_metrics(context, trajectory.final_metrics)
        context.n_agent_steps = sum(
            1 for step in trajectory.steps if step.source == "agent"
        )


# ------------------------------------------------------------------- TOML output


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return json.dumps(str(value))


def _to_toml(config: dict[str, Any], prefix: str = "") -> str:
    """Minimal TOML writer: scalars first, then nested tables.

    Sufficient for the config shape above (no arrays of tables, no dotted keys
    beyond one level of quoting).
    """
    scalars: list[str] = []
    tables: list[str] = []
    for key, value in config.items():
        quoted_key = key if _is_bare_key(key) else json.dumps(key)
        if isinstance(value, dict):
            path = f"{prefix}.{quoted_key}" if prefix else quoted_key
            body = _to_toml(value, path)
            tables.append(f"[{path}]\n{body}")
        else:
            scalars.append(f"{quoted_key} = {_toml_value(value)}")
    parts = ["\n".join(scalars) + "\n" if scalars else ""]
    parts.extend(tables)
    return "\n".join(part for part in parts if part)


def _is_bare_key(key: str) -> bool:
    return bool(key) and all(ch.isalnum() or ch in "_-" for ch in key)


__all__ = ["KimiCode", "KIMI_CODE_HOME"]

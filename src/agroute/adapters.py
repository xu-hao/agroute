"""AgentAdapter interface and dynamic dispatch registry for coding agents."""

from __future__ import annotations

import abc
from copy import deepcopy
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from .validation import AgRouteError


class AgentAdapter(abc.ABC):
    """Abstract interface defining agent-specific discovery, invocation, and quota behavior."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """The canonical identifier for the agent (e.g. 'agy', 'claude', 'codex')."""
        ...

    @property
    @abc.abstractmethod
    def display_name(self) -> str:
        """Human-readable display name for the agent."""
        ...

    @property
    @abc.abstractmethod
    def capture_adapter(self) -> str:
        """Identifier of the CLI output capture adapter for this agent."""
        ...

    def validate_native_candidate(self, model: str, where: str) -> None:
        """Validate agent-specific constraints on native candidate model IDs."""
        pass

    @abc.abstractmethod
    def discover_candidates(
        self,
        surface: str,
        *,
        refresh: bool = False,
        **kwargs: Any,
    ) -> list[dict]:
        """Discover available model candidate cells on the given surface ('native' or 'cli')."""
        ...

    @abc.abstractmethod
    def build_native_invocation(
        self,
        model: str,
        effort: str,
        *,
        prompt: str | None = None,
        role: str = "Task Worker",
        subagent_type: str | None = None,
        workspace: str = "inherit",
    ) -> dict:
        """Build the host agent's native tool invocation directive."""
        ...

    @abc.abstractmethod
    def build_cli_invocation(
        self,
        model: str,
        effort: str,
        *,
        prompt: str | None = None,
    ) -> dict:
        """Build the non-interactive CLI invocation command and delivery mode."""
        ...

    @abc.abstractmethod
    def discover_live_usage_patch(self) -> dict:
        """Discover live usage/rate-limit patch from local session/log files or probe cache."""
        ...

    @abc.abstractmethod
    def probe_usage(self, cwd: Path) -> dict | None:
        """Probe live usage using agent-specific zero-token probe or app server."""
        ...

    def resolve_quota_group(
        self,
        model_id: str,
        candidate: dict,
        effective_state: dict,
    ) -> tuple[str, dict, dict]:
        """Default quota group and tier resolution for standard models."""
        lower_id = model_id.lower()
        tokens = [tok for tok in re.split(r"[-_./:]+", lower_id) if tok]
        tiers = effective_state.get("tiers") or {}

        s5 = deepcopy(effective_state["session_5h"])
        w7 = deepcopy(effective_state["weekly_7d"])
        quota_group = "shared"

        specific_keys = [lower_id]
        if "tier" in candidate and str(candidate["tier"]).lower() not in specific_keys:
            specific_keys.append(str(candidate["tier"]).lower())
        for tok in tokens:
            if (
                len(tok) >= 3
                and tok not in ("gemini", "claude", "gpt", "low", "medium", "high", "xhigh", "max", "standard")
                and tok not in specific_keys
            ):
                specific_keys.append(tok)

        for key in specific_keys:
            if f"{key}-5h" in tiers:
                s5 = deepcopy(tiers[f"{key}-5h"])
                quota_group = key
            if f"{key}-weekly" in tiers:
                w7 = deepcopy(tiers[f"{key}-weekly"])
                quota_group = key
            elif key in tiers:
                w7 = deepcopy(tiers[key])
                quota_group = key

        return quota_group, s5, w7


class AgyAdapter(AgentAdapter):
    """Adapter for Google Antigravity (agy)."""

    @property
    def name(self) -> str:
        return "agy"

    @property
    def display_name(self) -> str:
        return "Antigravity"

    @property
    def capture_adapter(self) -> str:
        return "agy-print-v1"

    def validate_native_candidate(self, model: str, where: str) -> None:
        if model not in ("inherit", "flash_lite", "flash", "pro"):
            raise AgRouteError(
                f"{where}: Antigravity native subagent model must be one of inherit, flash_lite, flash, pro"
            )

    def discover_candidates(
        self,
        surface: str,
        *,
        refresh: bool = False,
        **kwargs: Any,
    ) -> list[dict]:
        if surface == "native":
            from .discovery import AGY_NATIVE_SUBAGENT_CANDIDATES
            return deepcopy(AGY_NATIVE_SUBAGENT_CANDIDATES)
        from .discovery import discover_agy_cli_candidates
        return discover_agy_cli_candidates(refresh=refresh)

    def build_native_invocation(
        self,
        model: str,
        effort: str,
        *,
        prompt: str | None = None,
        role: str = "Task Worker",
        subagent_type: str | None = None,
        workspace: str = "inherit",
    ) -> dict:
        params: dict[str, Any] = {
            "TypeName": subagent_type or "self",
            "Model": model,
            "Role": role,
            "Workspace": workspace,
        }
        if prompt is not None:
            params["Prompt"] = prompt
        return {
            "kind": "native_tool",
            "tool_name": "invoke_subagent",
            "parameters": params,
        }

    def build_cli_invocation(
        self,
        model: str,
        effort: str,
        *,
        prompt: str | None = None,
    ) -> dict:
        argv = [
            "agy",
            "--output-format",
            "stream-json",
            "--model",
            model,
            "--effort",
            effort,
            "-p",
        ]
        if prompt is not None:
            argv.append(prompt)
        return {
            "argv": argv,
            "prompt_delivery": "argv" if prompt is not None else "append_arg_or_stdin",
            "capture_adapter": self.capture_adapter,
        }

    def discover_live_usage_patch(self) -> dict:
        from .usage import _read_shared_probe_cache
        return _read_shared_probe_cache(self.name)

    def probe_usage(self, cwd: Path) -> dict | None:
        if shutil.which("agy"):
            try:
                proc = subprocess.run(
                    ["agy", "--output-format", "json", "-p", "/usage"],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
                if combined:
                    from .usage import parse_native_usage
                    return parse_native_usage("agy", combined)
            except (OSError, subprocess.TimeoutExpired, AgRouteError):
                pass
            return {"rate_limited": False}
        return None

    def resolve_quota_group(
        self,
        model_id: str,
        candidate: dict,
        effective_state: dict,
    ) -> tuple[str, dict, dict]:
        lower_id = model_id.lower()
        tokens = [tok for tok in re.split(r"[-_./:]+", lower_id) if tok]
        tiers = effective_state.get("tiers") or {}

        s5 = deepcopy(effective_state["session_5h"])
        w7 = deepcopy(effective_state["weekly_7d"])
        quota_group = "shared"

        # Multi-provider buckets hosted inside Antigravity (e.g. Claude / GPT-OSS vs Gemini)
        if any(t in ("claude", "gpt", "opus", "sonnet", "haiku", "oss") for t in tokens):
            if "3p-5h" in tiers:
                s5 = deepcopy(tiers["3p-5h"])
                quota_group = "3p"
            if "3p-weekly" in tiers:
                w7 = deepcopy(tiers["3p-weekly"])
                quota_group = "3p"
        elif any(t in ("gemini", "flash", "flash_lite", "pro", "argon") for t in tokens):
            if "gemini-5h" in tiers:
                s5 = deepcopy(tiers["gemini-5h"])
                quota_group = "gemini"
            if "gemini-weekly" in tiers:
                w7 = deepcopy(tiers["gemini-weekly"])
                quota_group = "gemini"

        # Specific model or family overrides (e.g. argon, opus, sonnet, pro, flash)
        specific_keys = [lower_id]
        if "tier" in candidate and str(candidate["tier"]).lower() not in specific_keys:
            specific_keys.append(str(candidate["tier"]).lower())
        for tok in tokens:
            if (
                len(tok) >= 3
                and tok not in ("gemini", "claude", "gpt", "low", "medium", "high", "xhigh", "max", "standard")
                and tok not in specific_keys
            ):
                specific_keys.append(tok)

        for key in specific_keys:
            if f"{key}-5h" in tiers:
                s5 = deepcopy(tiers[f"{key}-5h"])
                quota_group = key
            if f"{key}-weekly" in tiers:
                w7 = deepcopy(tiers[f"{key}-weekly"])
                quota_group = key
            elif key in tiers:
                w7 = deepcopy(tiers[key])
                quota_group = key

        if lower_id == "pro" and "pro" not in tiers and "argon" in tiers:
            w7 = deepcopy(tiers["argon"])
            quota_group = "argon"

        return quota_group, s5, w7


class ClaudeAdapter(AgentAdapter):
    """Adapter for Claude Code (claude)."""

    @property
    def name(self) -> str:
        return "claude"

    @property
    def display_name(self) -> str:
        return "Claude Code"

    @property
    def capture_adapter(self) -> str:
        return "claude-sdk-v1"

    def discover_candidates(
        self,
        surface: str,
        *,
        refresh: bool = False,
        **kwargs: Any,
    ) -> list[dict]:
        from .discovery import discover_claude_candidates
        return discover_claude_candidates(claude_home=kwargs.get("claude_home"))

    def build_native_invocation(
        self,
        model: str,
        effort: str,
        *,
        prompt: str | None = None,
        role: str = "Task Worker",
        subagent_type: str | None = None,
        workspace: str = "inherit",
    ) -> dict:
        params: dict[str, Any] = {
            "subagent_type": subagent_type or "general-purpose",
            "description": role,
            "model": model,
            "effort": effort,
        }
        if prompt is not None:
            params["prompt"] = prompt
        return {
            "kind": "native_tool",
            "tool_name": "Agent",
            "parameters": params,
        }

    def build_cli_invocation(
        self,
        model: str,
        effort: str,
        *,
        prompt: str | None = None,
    ) -> dict:
        argv = [
            "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--model",
            model,
            "--effort",
            effort,
        ]
        if prompt is not None:
            argv.extend(["--", prompt])
        return {
            "argv": argv,
            "prompt_delivery": "argv" if prompt is not None else "append_arg_or_stdin",
            "capture_adapter": self.capture_adapter,
        }

    def discover_live_usage_patch(self) -> dict:
        from .usage import _discover_claude_live_patch, _read_shared_probe_cache
        patch = _discover_claude_live_patch()
        if patch:
            return patch
        return _read_shared_probe_cache(self.name)

    def resolve_quota_group(self, model_id, candidate, effective_state):
        group, s5, w7 = super().resolve_quota_group(model_id, candidate, effective_state)
        # Claude Code reports may qualify the model's bucket by surface.
        tokens = [token for token in re.split(r"[-_./:]+", model_id.lower())
                  if token not in ("claude", "code") and len(token) >= 3]
        tokens.append(str(candidate.get("tier", "")).lower())
        for key, win in effective_state.get("tiers", {}).items():
            if any(token and key.startswith(token + "-") for token in tokens):
                if key.endswith("-5h"):
                    s5, group = deepcopy(win), key[:-3]
                elif key.endswith("-weekly"):
                    w7, group = deepcopy(win), key[:-7]
        return group, s5, w7

    def probe_usage(self, cwd: Path) -> dict | None:
        if not shutil.which("claude"):
            raise AgRouteError("usage probe: Claude CLI is not installed")
        try:
            proc = subprocess.run(
                ["claude", "-p", "/usage", "--output-format", "stream-json", "--verbose", "--no-session-persistence"],
                cwd=cwd, capture_output=True, text=True, timeout=15, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgRouteError(f"usage probe: Claude command failed: {exc}") from exc
        from .usage import parse_native_usage
        combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        # Cached transcripts are useful evidence, but cannot turn a failed
        # current probe into a fresh successful availability report.
        return parse_native_usage("claude", combined)


class CodexAdapter(AgentAdapter):
    """Adapter for OpenAI Codex CLI (codex)."""

    @property
    def name(self) -> str:
        return "codex"

    @property
    def display_name(self) -> str:
        return "Codex"

    @property
    def capture_adapter(self) -> str:
        return "codex-exec-v1"

    def discover_candidates(
        self,
        surface: str,
        *,
        refresh: bool = False,
        **kwargs: Any,
    ) -> list[dict]:
        from .discovery import discover_codex_candidates
        return discover_codex_candidates(codex_home=kwargs.get("codex_home"))

    def build_native_invocation(
        self,
        model: str,
        effort: str,
        *,
        prompt: str | None = None,
        role: str = "Task Worker",
        subagent_type: str | None = None,
        workspace: str = "inherit",
    ) -> dict:
        params: dict[str, Any] = {
            "model": model,
            "reasoning_effort": effort,
        }
        if subagent_type is not None:
            params["agent_type"] = subagent_type
        if prompt is not None:
            params["prompt"] = prompt
        return {
            "kind": "native_tool",
            "tool_name": "spawn_agent",
            "parameters": params,
        }

    def build_cli_invocation(
        self,
        model: str,
        effort: str,
        *,
        prompt: str | None = None,
    ) -> dict:
        argv = [
            "codex",
            "exec",
            "--json",
            "--model",
            model,
            "-c",
            f"model_reasoning_effort={effort}",
        ]
        if prompt is not None:
            argv.extend(["--", prompt])
        return {
            "argv": argv,
            "prompt_delivery": "argv_after_double_dash" if prompt is not None else "append_arg_or_stdin",
            "capture_adapter": self.capture_adapter,
        }

    def discover_live_usage_patch(self) -> dict:
        from .usage import _discover_codex_live_patch, _read_shared_probe_cache
        patch = _discover_codex_live_patch()
        if patch:
            return patch
        return _read_shared_probe_cache(self.name)

    def probe_usage(self, cwd: Path) -> dict | None:
        from .usage import _probe_codex_app_server
        rpc_patch = _probe_codex_app_server(cwd)
        if rpc_patch:
            return rpc_patch
        patch = self.discover_live_usage_patch()
        if patch:
            return patch
        return None


class AgentRegistry:
    """Registry of AgentAdapter instances supporting lookup and registration."""

    def __init__(self) -> None:
        self._adapters: dict[str, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter) -> None:
        """Register an adapter under its adapter.name."""
        self._adapters[adapter.name] = adapter

    def unregister(self, name: str) -> AgentAdapter | None:
        """Unregister an adapter by name (useful for test fixtures)."""
        return self._adapters.pop(name, None)

    def get(self, name: str) -> AgentAdapter:
        """Retrieve an adapter by agent name, raising AgRouteError if unknown."""
        adapter = self._adapters.get(name)
        if adapter is None:
            valid = ", ".join(repr(k) for k in sorted(self._adapters.keys()))
            raise AgRouteError(f"unsupported agent '{name}' (expected one of [{valid}])")
        return adapter

    def list(self) -> list[str]:
        """Return a sorted list of registered agent names."""
        return sorted(self._adapters.keys())

    def __contains__(self, name: str) -> bool:
        return name in self._adapters


default_registry = AgentRegistry()
default_registry.register(AgyAdapter())
default_registry.register(ClaudeAdapter())
default_registry.register(CodexAdapter())


def get_adapter(name: str) -> AgentAdapter:
    """Retrieve the registered AgentAdapter for name."""
    return default_registry.get(name)


def register_adapter(adapter: AgentAdapter) -> None:
    """Register an AgentAdapter into the default registry."""
    default_registry.register(adapter)


def list_adapters() -> list[str]:
    """Return the list of registered agent names."""
    return default_registry.list()

"""Unit tests for the AgentAdapter interface, dynamic dispatch, and registry (#15)."""

from pathlib import Path
import pytest

from agroute import AgRouteError, Router, get_adapter, list_adapters, register_adapter
from agroute.adapters import (
    AgentAdapter,
    AgyAdapter,
    ClaudeAdapter,
    CodexAdapter,
    default_registry,
)


def test_default_adapters_registered():
    names = list_adapters()
    assert "agy" in names
    assert "claude" in names
    assert "codex" in names

    assert isinstance(get_adapter("agy"), AgyAdapter)
    assert isinstance(get_adapter("claude"), ClaudeAdapter)
    assert isinstance(get_adapter("codex"), CodexAdapter)

    with pytest.raises(AgRouteError, match="unsupported agent 'unknown'"):
        get_adapter("unknown")


def test_adapter_properties():
    agy = get_adapter("agy")
    claude = get_adapter("claude")
    codex = get_adapter("codex")

    assert agy.name == "agy"
    assert agy.display_name == "Antigravity"
    assert agy.capture_adapter == "agy-print-v1"

    assert claude.name == "claude"
    assert claude.display_name == "Claude Code"
    assert claude.capture_adapter == "claude-sdk-v1"

    assert codex.name == "codex"
    assert codex.display_name == "Codex"
    assert codex.capture_adapter == "codex-exec-v1"


def test_validate_native_candidate():
    agy = get_adapter("agy")
    claude = get_adapter("claude")
    codex = get_adapter("codex")

    # Agy restricts native models to invoke_subagent enum
    for valid in ("inherit", "flash_lite", "flash", "pro"):
        agy.validate_native_candidate(valid, "where")

    with pytest.raises(AgRouteError, match="Antigravity native subagent model must be one of"):
        agy.validate_native_candidate("gemini-custom", "where")

    # Claude and Codex allow arbitrary bare model strings
    claude.validate_native_candidate("custom-claude", "where")
    codex.validate_native_candidate("custom-codex", "where")


def test_build_native_and_cli_invocation_dispatch():
    agy = get_adapter("agy")
    claude = get_adapter("claude")
    codex = get_adapter("codex")

    # Native invocations
    nat_agy = agy.build_native_invocation("pro", "standard", prompt="do stuff", role="Worker")
    assert nat_agy["kind"] == "native_tool"
    assert nat_agy["tool_name"] == "invoke_subagent"
    assert nat_agy["parameters"]["Prompt"] == "do stuff"
    assert nat_agy["parameters"]["TypeName"] == "self"

    nat_claude = claude.build_native_invocation("claude-sonnet-4-6", "high", prompt="review", role="Reviewer")
    assert nat_claude["kind"] == "native_tool"
    assert nat_claude["tool_name"] == "Agent"
    assert nat_claude["parameters"]["prompt"] == "review"
    assert nat_claude["parameters"]["description"] == "Reviewer"

    nat_codex = codex.build_native_invocation("gpt-5.4", "medium", prompt="build", subagent_type="coder")
    assert nat_codex["kind"] == "native_tool"
    assert nat_codex["tool_name"] == "spawn_agent"
    assert nat_codex["parameters"]["prompt"] == "build"
    assert nat_codex["parameters"]["agent_type"] == "coder"

    # CLI invocations
    cli_agy = agy.build_cli_invocation("gemini-2.5-pro", "high", prompt="audit")
    assert cli_agy["argv"] == [
        "agy",
        "--output-format",
        "stream-json",
        "--model",
        "gemini-2.5-pro",
        "--effort",
        "high",
        "-p",
        "audit",
    ]
    assert cli_agy["prompt_delivery"] == "argv"
    assert cli_agy["capture_adapter"] == "agy-print-v1"

    cli_claude = claude.build_cli_invocation("claude-opus-4-6", "medium", prompt="refactor")
    assert cli_claude["argv"] == [
        "claude",
        "-p",
        "--verbose",
        "--output-format",
        "stream-json",
        "--model",
        "claude-opus-4-6",
        "--effort",
        "medium",
        "--",
        "refactor",
    ]
    assert cli_claude["capture_adapter"] == "claude-sdk-v1"

    cli_codex = codex.build_cli_invocation("gpt-5.4", "high", prompt="test")
    assert cli_codex["argv"] == [
        "codex",
        "exec",
        "--json",
        "--model",
        "gpt-5.4",
        "-c",
        "model_reasoning_effort=high",
        "--",
        "test",
    ]
    assert cli_codex["capture_adapter"] == "codex-exec-v1"


def test_resolve_quota_group_specializations():
    agy = get_adapter("agy")
    claude = get_adapter("claude")

    effective_state = {
        "session_5h": {"used_pct": 10.0, "reset_at": None},
        "weekly_7d": {"used_pct": 20.0, "reset_at": None},
        "tiers": {
            "3p-5h": {"used_pct": 55.0, "reset_at": None},
            "gemini-5h": {"used_pct": 45.0, "reset_at": None},
            "argon": {"used_pct": 80.0, "reset_at": None},
        },
    }

    # 1. agy with 3p model routes to 3p bucket when no specific tier override exists
    group, s5, _ = agy.resolve_quota_group("claude-sonnet-4-6", {}, effective_state)
    assert group == "3p"
    assert s5["used_pct"] == 55.0

    # 2. agy with gemini model routes to gemini bucket
    group, s5, _ = agy.resolve_quota_group("gemini-2.5-flash", {}, effective_state)
    assert group == "gemini"
    assert s5["used_pct"] == 45.0

    # 3. agy with bare "pro" falls back to argon bucket when pro is absent
    group, _, w7 = agy.resolve_quota_group("pro", {}, effective_state)
    assert group == "argon"
    assert w7["used_pct"] == 80.0

    # 4. specific tier override takes precedence when present
    state_with_specific = {
        "session_5h": {"used_pct": 10.0, "reset_at": None},
        "weekly_7d": {"used_pct": 20.0, "reset_at": None},
        "tiers": {
            "3p-5h": {"used_pct": 55.0, "reset_at": None},
            "opus-5h": {"used_pct": 75.0, "reset_at": None},
        },
    }
    group_spec, s5_spec, _ = agy.resolve_quota_group("claude-opus-4-6", {}, state_with_specific)
    assert group_spec == "opus"
    assert s5_spec["used_pct"] == 75.0

    group_claude, s5_claude, _ = claude.resolve_quota_group("claude-opus-4-6", {}, state_with_specific)
    assert group_claude == "opus"
    assert s5_claude["used_pct"] == 75.0


def test_custom_agent_adapter_registration(tmp_path):
    class CustomAgentAdapter(AgentAdapter):
        @property
        def name(self) -> str:
            return "custom_bot"

        @property
        def display_name(self) -> str:
            return "Custom Bot"

        @property
        def capture_adapter(self) -> str:
            return "custom-bot-v1"

        def discover_candidates(self, surface: str, *, refresh: bool = False, **kwargs) -> list[dict]:
            return [{"model": "bot-fast", "effort": "low"}]

        def build_native_invocation(self, model: str, effort: str, *, prompt=None, **kwargs) -> dict:
            return {"kind": "native_tool", "tool_name": "custom_call", "parameters": {"prompt": prompt}}

        def build_cli_invocation(self, model: str, effort: str, *, prompt=None) -> dict:
            return {
                "argv": ["bot", "run", "--model", model, prompt or ""],
                "prompt_delivery": "argv",
                "capture_adapter": self.capture_adapter,
            }

        def discover_live_usage_patch(self) -> dict:
            return {}

        def probe_usage(self, cwd: Path) -> dict | None:
            return {"rate_limited": False}

    custom = CustomAgentAdapter()
    register_adapter(custom)
    try:
        assert "custom_bot" in list_adapters()
        adapter = get_adapter("custom_bot")
        assert adapter is custom

        cands = adapter.discover_candidates("cli")
        assert cands == [{"model": "bot-fast", "effort": "low"}]

        cli_plan = adapter.build_cli_invocation("bot-fast", "low", prompt="hello")
        assert cli_plan["argv"] == ["bot", "run", "--model", "bot-fast", "hello"]
    finally:
        default_registry.unregister("custom_bot")
    assert "custom_bot" not in list_adapters()

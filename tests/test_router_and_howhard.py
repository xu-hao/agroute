"""Tests for candidate emission, howhard 2.1+ inventories, and execution plan resolution (#4, #5)."""

import pytest

from agroute import AgRouteError, Router


def test_candidates_separate_native_host_from_cli_targets(tmp_path):
    router = Router(project=tmp_path)

    # When host is agy: agy uses native_candidates (pro, flash, flash_lite), codex & claude use cli_candidates
    res_agy = router.candidates("agy", now="2026-11-08T17:00:00Z")
    models_agy = [(c["model"], c["effort"]) for c in res_agy["candidates"]]
    assert ("agy/pro", "standard") in models_agy
    assert ("agy/gemini-2.5-pro", "high") not in models_agy
    assert ("codex/gpt-5.4", "high") in models_agy
    assert ("claude/claude-opus-4-6", "high") in models_agy

    # When host is codex: agy uses cli_candidates (gemini-2.5-pro, gemini-2.5-flash)
    res_codex = router.candidates("codex", now="2026-11-08T17:00:00Z")
    models_codex = [(c["model"], c["effort"]) for c in res_codex["candidates"]]
    assert ("agy/gemini-2.5-pro", "high") in models_codex
    assert ("agy/pro", "standard") not in models_codex


def test_tier_exclusion_and_host_reserve_filtering(tmp_path):
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        """
[agents.agy]
max_session_used_pct = 90.0
host_reserve_pct = 10.0
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=tmp_path)

    # Exclude Claude opus tier only; sonnet remains available
    router.usage_set("claude", tier="opus", tier_used_pct=96.0, now="2026-11-08T17:00:00Z")
    res = router.candidates("agy", now="2026-11-08T17:05:00Z")
    models = [c["model"] for c in res["candidates"]]
    assert "claude/claude-opus-4-6" not in models
    assert "claude/claude-sonnet-4-6" in models
    assert any(e["agent"] == "claude" and e["model"] == "claude-opus-4-6" for e in res["excluded"])

    # Set agy session_5h to 82%: blocked when agy is host (effective limit 90 - 10 = 80%),
    # but still eligible when agy is a non-host target (limit 90%)!
    router.usage_set("agy", session_used_pct=82.0, now="2026-11-08T17:05:00Z")
    res_host_agy = router.candidates("agy", now="2026-11-08T17:06:00Z")
    assert not any(c["model"].startswith("agy/") for c in res_host_agy["candidates"])

    res_host_codex = router.candidates("codex", now="2026-11-08T17:06:00Z")
    assert any(c["model"].startswith("agy/") for c in res_host_codex["candidates"])


def test_all_routes_exhausted_raises_diagnostic_error(tmp_path):
    router = Router(project=tmp_path)
    for agent in ("codex", "claude", "agy"):
        router.usage_set(agent, rate_limited=True, cooldown_until="2026-11-09T00:00:00Z", now="2026-11-08T17:00:00Z")
    with pytest.raises(AgRouteError, match="no eligible candidate routes available"):
        router.candidates("agy", now="2026-11-08T17:05:00Z")


def test_plan_resolution_native_and_cross_agent_across_all_hosts(tmp_path):
    router = Router(project=tmp_path)

    # 1. Host agy -> native agy/pro
    plan_agy_native = router.plan(
        "agy",
        {
            "decision_id": "dec-1",
            "chosen": {"model": "agy/pro", "effort": "standard"},
            "cap_met": True,
            "needs_review": False,
        },
        prompt="implement parser",
        role="Parser Builder",
    )
    assert plan_agy_native["execution_mode"] == "native"
    assert plan_agy_native["invocation"]["tool_name"] == "invoke_subagent"
    assert plan_agy_native["invocation"]["parameters"] == {
        "TypeName": "self",
        "Model": "pro",
        "Role": "Parser Builder",
        "Workspace": "inherit",
        "Prompt": "implement parser",
    }
    assert plan_agy_native["fallback"] == {
        "kind": "cli_command",
        "argv": [
            "agy",
            "--output-format",
            "stream-json",
            "--model",
            "pro",
            "--effort",
            "standard",
            "-p",
            "implement parser",
        ],
        "prompt_delivery": "argv",
        "output_capture_adapter": "agy-print-v1",
    }
    assert plan_agy_native["feedback"]["mode"] == "matched_receipt"

    # 2. Host agy -> cross-agent codex/gpt-5.4
    plan_agy_to_codex = router.plan(
        "agy",
        {
            "decision_id": "dec-2",
            "chosen": {"model": "codex/gpt-5.4", "effort": "high"},
            "cap_met": True,
            "needs_review": True,
        },
        prompt="fix bug",
    )
    assert plan_agy_to_codex["execution_mode"] == "cli"
    assert plan_agy_to_codex["fallback"] is None
    assert plan_agy_to_codex["needs_review"] is True
    assert plan_agy_to_codex["invocation"]["argv"] == [
        "codex",
        "exec",
        "--json",
        "--model",
        "gpt-5.4",
        "-c",
        "model_reasoning_effort=high",
        "--",
        "fix bug",
    ]
    assert plan_agy_to_codex["invocation"]["output_capture_adapter"] == "codex-exec-v1"

    # 3. Host claude -> native claude/claude-sonnet-4-6 and cross-agent agy/gemini-2.5-pro
    plan_claude_native = router.plan(
        "claude",
        {
            "decision_id": "dec-3",
            "chosen": {"model": "claude/claude-sonnet-4-6", "effort": "high"},
        },
        prompt="review PR",
    )
    assert plan_claude_native["execution_mode"] == "native"
    assert plan_claude_native["invocation"]["tool_name"] == "Agent"
    assert plan_claude_native["invocation"]["parameters"]["model"] == "claude-sonnet-4-6"

    plan_claude_to_agy = router.plan(
        "claude",
        {
            "decision_id": "dec-4",
            "chosen": {"model": "agy/gemini-2.5-pro", "effort": "high"},
        },
        prompt="audit",
    )
    assert plan_claude_to_agy["execution_mode"] == "cli"
    assert plan_claude_to_agy["invocation"]["argv"] == [
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

    # 4. Host codex -> native codex/gpt-5.4
    plan_codex_native = router.plan(
        "codex",
        {
            "decision_id": "dec-5",
            "chosen": {"model": "codex/gpt-5.4", "effort": "high"},
        },
        prompt="refactor",
    )
    assert plan_codex_native["execution_mode"] == "native"
    assert plan_codex_native["invocation"]["tool_name"] == "spawn_agent"
    assert plan_codex_native["invocation"]["parameters"]["reasoning_effort"] == "high"
    assert plan_codex_native["fallback"] == {
        "kind": "cli_command",
        "argv": [
            "codex",
            "exec",
            "--json",
            "--model",
            "gpt-5.4",
            "-c",
            "model_reasoning_effort=high",
            "--",
            "refactor",
        ],
        "prompt_delivery": "argv_after_double_dash",
        "output_capture_adapter": "codex-exec-v1",
    }


def test_host_affinity_epsilon_override_switches_feedback_to_raw_native(tmp_path):
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        """
[routing]
default_host = "agy"
host_affinity_epsilon = 0.50
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=tmp_path)

    decision = {
        "decision_id": "dec-affinity",
        "chosen": {"model": "codex/gpt-5.4", "effort": "high"},
        "cap_met": True,
        "needs_review": False,
        "candidates": [
            {"model": "codex/gpt-5.4", "effort": "high", "J": 2.00, "cap_met": True},
            {"model": "agy/pro", "effort": "standard", "J": 2.35, "cap_met": True},
        ],
    }
    plan = router.plan("agy", decision, prompt="nearby native cost")
    assert plan["host_affinity_applied"] is True
    assert plan["execution_mode"] == "native"
    assert plan["target_agent"] == "agy"
    assert plan["howhard_cell"] == {"model": "agy/pro", "effort": "standard"}
    assert plan["feedback"]["mode"] == "raw_native"
    assert plan["feedback"]["decision_id"] is None
    assert plan["feedback"]["unexecuted_decision_id"] == "dec-affinity"

    # When native cell exceeds epsilon (delta = 0.80 > 0.50), keep chosen cross-agent cell
    decision_far = {
        "decision_id": "dec-far",
        "chosen": {"model": "codex/gpt-5.4", "effort": "high"},
        "candidates": [
            {"model": "codex/gpt-5.4", "effort": "high", "J": 2.00, "cap_met": True},
            {"model": "agy/pro", "effort": "standard", "J": 2.80, "cap_met": True},
        ],
    }
    plan_far = router.plan("agy", decision_far, prompt="far native cost")
    assert plan_far["host_affinity_applied"] is False
    assert plan_far["execution_mode"] == "cli"
    assert plan_far["target_agent"] == "codex"
    assert plan_far["feedback"]["mode"] == "matched_receipt"


def test_howhard_inventory_toml_generation(tmp_path):
    import tomllib

    router = Router(project=tmp_path)
    toml_str = router.format_inventory_toml("agy", name="router")
    parsed = tomllib.loads(toml_str)
    assert "inventories" in parsed
    assert "router" in parsed["inventories"]
    assert any(c["model"] == "agy/pro" for c in parsed["inventories"]["router"])

"""Behavioral tests for subscription plan presets, quota-group exclusions, and 5h + weekly credit budgets (#37)."""

import json
import os
from pathlib import Path
import time
import tomllib

import pytest

from agroute import AgRouteError, Router, load_config
from agroute.cli import main

NOW = "2026-11-12T12:00:00Z"


def _write_agy_models_cache(candidates):
    spool_dir = Path(os.environ["AGROUTE_SPOOL_DIR"])
    (spool_dir / "agy_models_cache.json").write_text(
        json.dumps(
            {
                "format": "agroute-agy-models-cache-v1",
                "fetched_at": time.time(),
                "candidates": candidates,
            }
        ),
        encoding="utf-8",
    )


def _write_claude_catalog(models):
    path = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "cache/model-catalog/catalog.json"
    entries = [
        {
            "id": m,
            "section": "main",
            "quick_select": True,
            "runtime": {"effort_levels": ["high"], "default_effort": "high"},
        }
        for m in models
    ]
    path.write_text(json.dumps({"catalog": {"config": {"models": entries}}}), encoding="utf-8")


def _write_codex_cache(models):
    path = Path(os.environ["CODEX_HOME"]) / "models_cache.json"
    entries = [
        {
            "slug": m,
            "visibility": "list",
            "priority": i + 1,
            "default_reasoning_level": "high",
            "supported_reasoning_levels": [{"effort": "high"}],
        }
        for i, m in enumerate(models)
    ]
    path.write_text(json.dumps({"models": entries}), encoding="utf-8")


@pytest.mark.parametrize(
    "agent,raw_plan,expected_canonical,expected_credit",
    [
        (
            "agy",
            "pro-20",
            "pro-20",
            {
                "tiers": {
                    "gemini": {"session_5h_usd": 32.0, "weekly_7d_usd": 135.0},
                    "3p": {"session_5h_usd": 5.0, "weekly_7d_usd": 10.0},
                }
            },
        ),
        (
            "agy",
            "  Google-AI-Pro ",
            "pro-20",
            {
                "tiers": {
                    "gemini": {"session_5h_usd": 32.0, "weekly_7d_usd": 135.0},
                    "3p": {"session_5h_usd": 5.0, "weekly_7d_usd": 10.0},
                }
            },
        ),
        (
            "agy",
            "ultra-200",
            "ultra-200",
            {
                "tiers": {
                    "gemini": {"session_5h_usd": 320.0, "weekly_7d_usd": 1350.0},
                    "3p": {"session_5h_usd": 25.0, "weekly_7d_usd": 50.0},
                }
            },
        ),
        (
            "agy",
            "ultra",
            "ultra-200",
            {
                "tiers": {
                    "gemini": {"session_5h_usd": 320.0, "weekly_7d_usd": 1350.0},
                    "3p": {"session_5h_usd": 25.0, "weekly_7d_usd": 50.0},
                }
            },
        ),
        (
            "claude",
            "claude-pro",
            "pro-20",
            {"session_5h_usd": 18.0, "weekly_7d_usd": 45.0, "tiers": {}},
        ),
        (
            "claude",
            "claude-max-5x",
            "max-5x",
            {
                "session_5h_usd": 90.0,
                "weekly_7d_usd": 225.0,
                "tiers": {"fable": {"weekly_7d_usd": 75.0}},
            },
        ),
        (
            "claude",
            "max-20x",
            "max-20x",
            {
                "session_5h_usd": 360.0,
                "weekly_7d_usd": 900.0,
                "tiers": {"fable": {"weekly_7d_usd": 300.0}},
            },
        ),
        (
            "codex",
            "chatgpt-plus",
            "plus-20",
            {"session_5h_usd": 20.0, "weekly_7d_usd": 50.0, "tiers": {}},
        ),
        (
            "codex",
            "pro-200",
            "pro-200",
            {"session_5h_usd": 200.0, "weekly_7d_usd": 500.0, "tiers": {}},
        ),
        (
            "codex",
            "pro",
            "pro-200",
            {"session_5h_usd": 200.0, "weekly_7d_usd": 500.0, "tiers": {}},
        ),
    ],
)
def test_plan_presets_and_aliases_resolve_correctly(
    tmp_path, agent, raw_plan, expected_canonical, expected_credit
):
    cfg_file = tmp_path / "agroute.toml"
    cfg_file.write_text(f'[agents.{agent}]\nplan = "{raw_plan}"\n', encoding="utf-8")
    cfg = load_config(cfg_file)
    assert cfg.agents[agent]["plan"] == expected_canonical
    assert cfg.agents[agent]["credit"] == expected_credit


def test_layered_plan_and_credit_overrides_merge_cleanly(tmp_path, monkeypatch):
    user_cfg = tmp_path / "user-config.toml"
    user_cfg.write_text(
        """
[agents.agy]
plan = "pro-20"
exclude_quota_groups = ["3p", "argon"]

[agents.agy.credit.tiers.3p]
session_5h_usd = 12.0

[agents.claude]
plan = "max-5x"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGROUTE_USER_CONFIG", str(user_cfg))

    proj_dir = tmp_path / "project"
    proj_dir.mkdir()
    (proj_dir / "agroute.toml").write_text(
        """
[agents.agy]
plan = "ultra-200"
exclude_quota_groups = ["3p"]

[agents.agy.credit.tiers.gemini]
weekly_7d_usd = 1500.0

[agents.claude.credit]
session_5h_usd = 100.0
""",
        encoding="utf-8",
    )

    cfg = load_config(project=proj_dir)
    assert cfg.agents["agy"]["plan"] == "ultra-200"
    assert cfg.agents["agy"]["exclude_quota_groups"] == ["3p"]
    # User 3p session_5h_usd override + project gemini weekly_7d_usd override layered over ultra-200
    assert cfg.agents["agy"]["credit"] == {
        "tiers": {
            "gemini": {"session_5h_usd": 320.0, "weekly_7d_usd": 1500.0},
            "3p": {"session_5h_usd": 12.0, "weekly_7d_usd": 50.0},
        }
    }
    assert cfg.agents["claude"]["plan"] == "max-5x"
    assert cfg.agents["claude"]["credit"] == {
        "session_5h_usd": 100.0,
        "weekly_7d_usd": 225.0,
        "tiers": {"fable": {"weekly_7d_usd": 75.0}},
    }


@pytest.mark.parametrize(
    "bad_toml,match",
    [
        ('[agents.agy]\nplan = "enterprise-999"\n', "unknown plan 'enterprise-999' for agent 'agy'"),
        ('[agents.codex]\nplan = "ultra-200"\n', "unknown plan 'ultra-200' for agent 'codex'"),
        ("[agents.claude]\nplan = 20\n", "agents.claude.plan: expected a string"),
        ("[agents.claude.credit]\nsession_5h_usd = 0\n", "expected a positive number"),
        ("[agents.claude.credit]\nweekly_7d_usd = -5.0\n", "expected a positive number"),
        ("[agents.claude.credit]\nsession_5h_usd = true\n", "expected a finite number"),
        ("[agents.claude.credit]\nunknown_field = 10.0\n", "unknown keys"),
        ("[agents.agy.credit.tiers.gemini]\n", "expected at least one of session_5h_usd or weekly_7d_usd"),
        ("[agents.agy.credit.tiers.gemini]\nextra = 1.0\n", "unknown keys"),
        ('[agents.agy]\nexclude_quota_groups = "3p"\n', "expected a list of quota-group labels"),
        ('[agents.agy]\nexclude_quota_groups = [""]\n', "expected a non-empty trimmed string"),
    ],
)
def test_invalid_plan_credit_or_exclude_quota_groups_refused(tmp_path, bad_toml, match):
    cfg_file = tmp_path / "agroute.toml"
    cfg_file.write_text(bad_toml, encoding="utf-8")
    with pytest.raises(AgRouteError, match=match):
        load_config(cfg_file)


def test_exclude_quota_groups_filters_agy_3p_while_keeping_dynamic_gemini_discovery(tmp_path):
    _write_agy_models_cache(
        [
            {"model": "gemini-4-carbon-high", "effort": "high"},
            {"model": "gemini-3.8-flash-medium", "effort": "medium"},
            {"model": "claude-opus-5-5-high", "effort": "high"},
            {"model": "gpt-oss-120b-medium", "effort": "medium"},
        ]
    )
    cfg_file = tmp_path / "agroute.toml"
    cfg_file.write_text(
        """
[agents.agy]
plan = "ultra-200"
exclude_quota_groups = ["3p"]
""",
        encoding="utf-8",
    )
    router = Router(cfg_file, project=tmp_path)
    res = router.candidates("agy", include_agents=["agy"], now=NOW)
    admitted_models = [c["model"] for c in res["candidates"]]
    assert admitted_models == ["agy/gemini-4-carbon-high", "agy/gemini-3.8-flash-medium"]

    excluded_by_model = {e["model"]: e["reason"] for e in res["excluded"]}
    assert excluded_by_model["claude-opus-5-5-high"] == "quota_group '3p' is excluded in configuration"
    assert excluded_by_model["gpt-oss-120b-medium"] == "quota_group '3p' is excluded in configuration"

    snap = router.usage_show("agy", now=NOW)["agents"]["agy"]
    assert snap["plan"] == "ultra-200"
    assert snap["models"]["claude-opus-5-5-high"]["eligible"] is False
    assert (
        snap["models"]["claude-opus-5-5-high"]["exclusion_reason"]
        == "quota_group '3p' is excluded in configuration"
    )
    assert snap["models"]["gemini-4-carbon-high"]["eligible"] is True


def test_5h_and_weekly_credit_budget_math_host_reserve_unpriced_exclusion_and_inventory_toml(tmp_path):
    # Include an unpriced preview model alongside priced Gemini, Claude (Fable + Opus), and Codex models
    _write_agy_models_cache(
        [
            {"model": "gemini-4-carbon-high", "effort": "high"},
            {"model": "gemini-preview-unpriced-high", "effort": "high"},
        ]
    )
    _write_claude_catalog(["claude-opus-5-5", "claude-fable-5-2"])
    _write_codex_cache(["gpt-6.1-sol"])

    cfg_file = tmp_path / "agroute.toml"
    cfg_file.write_text(
        """
[agents.agy]
plan = "ultra-200"
host_reserve_pct = 10.0

[agents.claude]
plan = "max-20x"

[agents.codex]
plan = "plus-20"
""",
        encoding="utf-8",
    )
    router = Router(cfg_file, project=tmp_path)

    # 1. Agy: gemini-5h used 25%, gemini-weekly used 80%.
    # When agy is host (reserve = 10%):
    #   pct_left_5h = 100 - 10 - 25 = 65% -> 0.65 * 320 = 208.0 USD
    #   pct_left_7d = 100 - 10 - 80 = 10% -> 0.10 * 1350 = 135.0 USD (7d is tighter!)
    # When agy is NOT host (reserve = 0%):
    #   pct_left_5h = 100 - 25 = 75% -> 0.75 * 320 = 240.0 USD
    #   pct_left_7d = 100 - 80 = 20% -> 0.20 * 1350 = 270.0 USD (5h is tighter!)
    agy_report = json.dumps(
        {
            "status": "SUCCESS",
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-5h",
                                    "window": "5h",
                                    "remaining_fraction": 0.75,
                                    "reset_time": "2026-11-12T16:00:00Z",
                                },
                                {
                                    "id": "gemini-weekly",
                                    "window": "weekly",
                                    "remaining_fraction": 0.20,
                                    "reset_time": "2026-11-18T00:00:00Z",
                                },
                            ],
                        }
                    ]
                },
            },
        }
    )
    router.usage_ingest("agy", agy_report, now=NOW)

    # 2. Claude (max-20x: 5h=360, 7d=900, fable 7d=300):
    #   session_5h used 50% -> 180.0 USD left
    #   weekly_7d used 10% -> 810.0 USD left for opus (5h is tighter: available=180.0)
    #   fable weekly used 60% -> 40% * 300 = 120.0 USD left for fable (7d is tighter: available=120.0)
    router.usage_set(
        "claude",
        session_used_pct=50.0,
        weekly_used_pct=10.0,
        tier="fable",
        tier_used_pct=60.0,
        now=NOW,
    )

    # 3. Codex (plus-20: 5h=20, 7d=50): only session_5h reported (20% used -> 16.0 USD), weekly_7d is None
    router.usage_set("codex", session_used_pct=20.0, now=NOW)

    res_host_agy = router.candidates("agy", now=NOW)
    by_model = {c["model"]: c for c in res_host_agy["candidates"]}

    # Unpriced preview model is excluded when credit_budget is active!
    assert "agy/gemini-preview-unpriced-high" not in by_model
    assert any(
        e["agent"] == "agy"
        and e["model"] == "gemini-preview-unpriced-high"
        and e["reason"] == "model has no known price required for credit_budget evaluation"
        for e in res_host_agy["excluded"]
    )

    assert by_model["agy/gemini-4-carbon-high"]["credit_budget"] == {
        "available_usd": 135.0,
        "session_5h_remaining_usd": 208.0,
        "weekly_7d_remaining_usd": 135.0,
    }
    assert by_model["claude/claude-opus-5-5"]["credit_budget"] == {
        "available_usd": 180.0,
        "session_5h_remaining_usd": 180.0,
        "weekly_7d_remaining_usd": 810.0,
    }
    assert by_model["claude/claude-fable-5-2"]["credit_budget"] == {
        "available_usd": 120.0,
        "session_5h_remaining_usd": 180.0,
        "weekly_7d_remaining_usd": 120.0,
    }
    # Codex has no weekly_7d_remaining_usd key (no None values in credit_budget)
    assert by_model["codex/gpt-6.1-sol"]["credit_budget"] == {
        "available_usd": 16.0,
        "session_5h_remaining_usd": 16.0,
    }

    # Check non-host agy (reserve not deducted when host is codex)
    res_host_codex = router.candidates("codex", now=NOW)
    by_model_codex_host = {c["model"]: c for c in res_host_codex["candidates"]}
    assert by_model_codex_host["agy/gemini-4-carbon-high"]["credit_budget"] == {
        "available_usd": 240.0,
        "session_5h_remaining_usd": 240.0,
        "weekly_7d_remaining_usd": 270.0,
    }

    # Verify TOML inventory rendering with inline credit_budget tables parses cleanly
    toml_str = router.format_inventory_toml("agy", name="router", now=NOW)
    parsed = tomllib.loads(toml_str)
    inv_by_model = {c["model"]: c for c in parsed["inventories"]["router"]}
    assert inv_by_model["agy/gemini-4-carbon-high"]["credit_budget"] == {
        "available_usd": 135.0,
        "session_5h_remaining_usd": 208.0,
        "weekly_7d_remaining_usd": 135.0,
    }
    assert inv_by_model["codex/gpt-6.1-sol"]["credit_budget"] == {
        "available_usd": 16.0,
        "session_5h_remaining_usd": 16.0,
    }


def test_zero_remaining_credit_excludes_model(tmp_path):
    _write_agy_models_cache([{"model": "gemini-4-carbon-high", "effort": "high"}])
    cfg_file = tmp_path / "agroute.toml"
    cfg_file.write_text(
        """
[agents.agy]
plan = "pro-20"
max_session_used_pct = 95.0
host_reserve_pct = 10.0
""",
        encoding="utf-8",
    )
    router = Router(cfg_file, project=tmp_path)
    # 90% used on gemini-5h + 10% host reserve = 0% left -> 0 USD remaining
    router.usage_set("agy", tier="gemini-5h", tier_used_pct=90.0, now=NOW)
    snap = router.usage_show("agy", now=NOW)["agents"]["agy"]
    mq = snap["models"]["gemini-4-carbon-high"]
    # Note: in usage_show, cli surface comes first (is_host=False -> 3.2 USD),
    # while candidates("agy") evaluates native surface with is_host=True -> 0.0 USD!
    assert mq["credit_budget"]["available_usd"] == 3.2

    with pytest.raises(AgRouteError, match="0 USD remaining"):
        router.candidates("agy", include_agents=["agy"], now=NOW)


def test_plan_enforces_live_credit_budget_and_host_affinity(tmp_path, capsys):
    _write_agy_models_cache([{"model": "gemini-4-carbon-high", "effort": "high"}])
    _write_claude_catalog(["claude-opus-5-5"])
    _write_codex_cache(["gpt-6.1-sol"])

    cfg_file = tmp_path / "agroute.toml"
    cfg_file.write_text(
        """
[routing]
default_host = "agy"
host_affinity_epsilon = 0.50

[agents.agy]
plan = "pro-20"

[agents.claude]
plan = "pro-20"

[agents.codex]
plan = "plus-20"
""",
        encoding="utf-8",
    )
    router = Router(cfg_file, project=tmp_path)
    # Set initial usage:
    # claude pro-20: 5h=18.0, 7d=45.0. With 50% 5h used -> 9.0 USD available.
    # agy pro-20 gemini: 5h=32.0, 7d=135.0. With 90% 5h used (Wait: max_session_used_pct=90, so use 80%)
    # With 80% gemini-5h used -> 20% * 32.0 = 6.4 USD available.
    router.usage_set("claude", session_used_pct=50.0, weekly_used_pct=10.0, now=NOW)
    router.usage_set("agy", tier="gemini-5h", tier_used_pct=80.0, now=NOW)
    router.usage_set("codex", session_used_pct=10.0, now=NOW)

    # 1. Decision requiring 4.5 USD on claude/claude-opus-5-5 (available 9.0 USD) succeeds.
    # Even though agy/gemini-4-carbon-high is within host_affinity_epsilon (J=2.3 vs 2.0),
    # agy requires 8.0 USD > 6.4 USD available, so host affinity MUST NOT substitute to agy!
    decision = {
        "decision_id": "dec-credit-ok",
        "chosen": {"model": "claude/claude-opus-5-5", "effort": "high"},
        "cap_met": True,
        "needs_review": False,
        "cells": [
            {
                "model": "claude/claude-opus-5-5",
                "effort": "high",
                "J": 2.0,
                "needed_credit_usd": 4.5,
                "cap_met": True,
            },
            {
                "model": "agy/gemini-4-carbon-high",
                "effort": "high",
                "J": 2.3,
                "needed_credit_usd": 8.0,
                "cap_met": True,
            },
        ],
    }
    plan_ok = router.plan("agy", decision, prompt="run within credit", now=NOW)
    assert plan_ok["host_affinity_applied"] is False
    assert plan_ok["target_agent"] == "claude"
    assert plan_ok["needed_credit_usd"] == 4.5
    assert plan_ok["credit_budget"] == {
        "available_usd": 9.0,
        "session_5h_remaining_usd": 9.0,
        "weekly_7d_remaining_usd": 40.5,
    }
    shown = router.show_route(plan_ok["route_id"], now=NOW)
    assert shown["needed_credit_usd"] == 4.5
    assert shown["credit_budget"]["available_usd"] == 9.0

    # 2. Now live usage on claude moves to 85% used on session_5h -> 15% * 18.0 = 2.7 USD remaining.
    # Re-running plan with needed_credit_usd = 4.5 > 2.7 raises AgRouteError!
    router.usage_set("claude", session_used_pct=85.0, weekly_used_pct=10.0, now=NOW)
    with pytest.raises(
        AgRouteError,
        match=r"requires 4\.5 USD credit, exceeding remaining credit 2\.7 USD \(session_5h_remaining_usd=2\.7, weekly_7d_remaining_usd=40\.5\)",
    ):
        router.plan("agy", decision, prompt="exceeds live credit", now=NOW)

    # 3. Also verify C_run fallback when needed_credit_usd is absent
    decision_c_run = {
        "decision_id": "dec-c-run",
        "chosen": {"model": "claude/claude-opus-5-5", "effort": "high"},
        "candidates": [
            {
                "model": "claude/claude-opus-5-5",
                "effort": "high",
                "J": 2.0,
                "C_run": 3.5,
                "cap_met": True,
            }
        ],
    }
    with pytest.raises(AgRouteError, match=r"requires 3\.5 USD credit, exceeding remaining credit 2\.7 USD"):
        router.plan("agy", decision_c_run, prompt="c_run fallback", now=NOW)

"""Behavioral tests for credit-denominated reserves in every quota window (#67)."""

import json
import os
from pathlib import Path
import re
import time

import pytest

from agroute import AgRouteError, Router, load_config
from agroute.cli import main

NOW = "2026-11-18T12:00:00Z"
# The howhard candidate contract accepts only these credit_budget keys.
HOWHARD_BUDGET_KEYS = {"available_credit_usd", "session_5h_remaining_credit_usd", "weekly_7d_remaining_credit_usd"}


def _write_codex_cache(models):
    entries = [
        {
            "slug": model,
            "visibility": "list",
            "priority": index + 1,
            "default_reasoning_level": "high",
            "supported_reasoning_levels": [{"effort": "high"}],
        }
        for index, model in enumerate(models)
    ]
    (Path(os.environ["CODEX_HOME"]) / "models_cache.json").write_text(
        json.dumps({"models": entries}), encoding="utf-8"
    )


def _write_claude_catalog(models):
    entries = [
        {
            "id": model,
            "section": "main",
            "quick_select": True,
            "runtime": {"effort_levels": ["high"], "default_effort": "high"},
        }
        for model in models
    ]
    (Path(os.environ["CLAUDE_CONFIG_DIR"]) / "cache/model-catalog/catalog.json").write_text(
        json.dumps({"catalog": {"config": {"models": entries}}}), encoding="utf-8"
    )


def _write_agy_models_cache(candidates):
    (Path(os.environ["AGROUTE_SPOOL_DIR"]) / "agy_models_cache.json").write_text(
        json.dumps(
            {"format": "agroute-agy-models-cache-v1", "fetched_at": time.time(), "candidates": candidates}
        ),
        encoding="utf-8",
    )


def _router(tmp_path, toml_text, name="agroute.toml"):
    # Routers built for the same project share one usage store.
    path = tmp_path / name
    path.write_text(toml_text, encoding="utf-8")
    return Router(path, project=tmp_path)


def _cell(result, model):
    return next(cell for cell in result["candidates"] if cell["model"] == model)


def _reasons(result, agent):
    return [entry["reason"] for entry in result["excluded"] if entry["agent"] == agent]


def _reserve_warnings(result):
    return [warning for warning in result["warnings"] if "credit reserve" in warning]


def test_credit_reserve_excludes_a_candidate_that_percentage_limits_admit(tmp_path):
    _write_codex_cache(["gpt-6.1-sol"])
    _write_claude_catalog(["claude-opus-5-5"])
    percentage_only = _router(
        tmp_path, '[agents.codex]\nplan = "plus-20"\n\n[agents.claude]\nplan = "max-5x"\n', "percentage.toml"
    )
    # plus-20 has a 20 USD session: 80% used is below the 90% session limit and leaves 4 USD.
    percentage_only.usage_set("codex", session_used_pct=80.0, weekly_used_pct=10.0, now=NOW)
    percentage_only.usage_set("claude", session_used_pct=10.0, weekly_used_pct=10.0, now=NOW)
    admitted = percentage_only.candidates("agy", include_agents=["codex", "claude"], now=NOW)
    assert _cell(admitted, "codex/gpt-6.1-sol")["credit_budget"] == {
        "available_credit_usd": 4.0,
        "session_5h_remaining_credit_usd": 4.0,
        "weekly_7d_remaining_credit_usd": 45.0,
    }

    reserved = _router(
        tmp_path,
        '[agents.codex]\nplan = "plus-20"\nmin_remaining_credit_usd = 5.0\n\n[agents.claude]\nplan = "max-5x"\n',
        "reserved.toml",
    )
    result = reserved.candidates("agy", include_agents=["codex", "claude"], now=NOW)
    assert not [cell for cell in result["candidates"] if cell["model"].startswith("codex/")]
    assert _reasons(result, "codex") == [
        "session_5h credit_budget exhausted (0 USD remaining after 5 USD credit reserve)"
    ]
    assert _cell(result, "claude/claude-opus-5-5")["credit_budget"]["available_credit_usd"] == 81.0


def test_plan_refuses_needed_credit_that_fits_only_without_the_reserve(tmp_path):
    _write_codex_cache(["gpt-6.1-sol"])
    router = _router(tmp_path, '[agents.codex]\nplan = "plus-20"\nmin_remaining_credit_usd = 4.0\n')
    # 50% of the 20 USD session leaves 10 USD, and 6 USD after the 4 USD reserve.
    router.usage_set("codex", session_used_pct=50.0, weekly_used_pct=10.0, now=NOW)
    decision = {
        "decision_id": "dec-credit-reserve",
        "chosen": {"model": "codex/gpt-6.1-sol", "effort": "high"},
        "cap_met": True,
        "needs_review": False,
        "cells": [
            {"model": "codex/gpt-6.1-sol", "effort": "high", "J": 1.0, "needed_credit_usd": 7.0, "cap_met": True}
        ],
    }
    expected = (
        "requires 7 USD credit, exceeding remaining credit 6 USD (session_5h_remaining_credit_usd=6, "
        "weekly_7d_remaining_credit_usd=41, reserve_credit_usd=4)"
    )
    with pytest.raises(AgRouteError, match=re.escape(expected)):
        router.plan("agy", decision, prompt="over the reserve", include_agents=["codex"], now=NOW)

    decision["cells"][0]["needed_credit_usd"] = 5.5
    plan = router.plan("agy", decision, prompt="within the reserve", include_agents=["codex"], now=NOW)
    assert plan["needed_credit_usd"] == 5.5
    assert plan["credit_budget"] == {
        "available_credit_usd": 6.0,
        "session_5h_remaining_credit_usd": 6.0,
        "weekly_7d_remaining_credit_usd": 41.0,
    }


@pytest.mark.parametrize(
    "session_used_pct,reserve,reasons,available",
    [
        # Percentage limit admits (80% < 90%); 4 USD left cannot keep a 5 USD reserve.
        (80.0, 5.0, ["session_5h credit_budget exhausted (0 USD remaining after 5 USD credit reserve)"], None),
        # The reserve leaves 1.3 of 1.8 USD, but 91% reaches the 90% session limit.
        (91.0, 0.5, ["session_5h used_pct (91.0%) >= effective limit (90.0%)"], None),
        # Both rules admit: 10 USD session minus the 5 USD reserve.
        (50.0, 5.0, [], 5.0),
    ],
)
def test_stricter_of_percentage_limit_and_credit_reserve_decides(
    tmp_path, session_used_pct, reserve, reasons, available
):
    _write_codex_cache(["gpt-6.1-sol"])
    _write_claude_catalog(["claude-opus-5-5"])
    router = _router(
        tmp_path,
        f'[agents.codex]\nplan = "plus-20"\nmin_remaining_credit_usd = {reserve}\n\n'
        '[agents.claude]\nplan = "max-5x"\n',
    )
    router.usage_set("codex", session_used_pct=session_used_pct, weekly_used_pct=10.0, now=NOW)
    router.usage_set("claude", session_used_pct=10.0, weekly_used_pct=10.0, now=NOW)
    result = router.candidates("agy", include_agents=["codex", "claude"], now=NOW)
    assert _reasons(result, "codex") == reasons
    if available is None:
        assert not [cell for cell in result["candidates"] if cell["model"].startswith("codex/")]
    else:
        assert _cell(result, "codex/gpt-6.1-sol")["credit_budget"]["available_credit_usd"] == available


def test_host_credit_reserve_applies_only_when_the_agent_is_the_routing_root(tmp_path):
    _write_codex_cache(["gpt-6.1-sol"])
    router = _router(
        tmp_path,
        '[agents.codex]\nplan = "plus-20"\nhost_reserve_pct = 10.0\n'
        "host_reserve_credit_usd = 3.0\nmin_remaining_credit_usd = 1.0\n",
    )
    router.usage_set("codex", session_used_pct=50.0, weekly_used_pct=20.0, now=NOW)

    as_root = router.candidates("codex", include_agents=["codex"], now=NOW)
    # Root: the percentage reserve and both credit amounts apply.
    # Session 20 * (100 - 50 - 10)% - (3 + 1) = 4; weekly 50 * (100 - 20 - 10)% - 4 = 31.
    assert _cell(as_root, "codex/gpt-6.1-sol")["credit_budget"] == {
        "available_credit_usd": 4.0,
        "session_5h_remaining_credit_usd": 4.0,
        "weekly_7d_remaining_credit_usd": 31.0,
    }
    root_windows = as_root["credit_calibration"]["codex"]["shared"]
    assert root_windows["session_5h"]["reserve_credit_usd"] == 4.0
    assert root_windows["weekly_7d"]["reserve_credit_usd"] == 4.0

    as_target = router.candidates("agy", include_agents=["codex"], now=NOW)
    # Another root: only the margin applies. Session 20 * 50% - 1 = 9; weekly 50 * 80% - 1 = 39.
    assert _cell(as_target, "codex/gpt-6.1-sol")["credit_budget"] == {
        "available_credit_usd": 9.0,
        "session_5h_remaining_credit_usd": 9.0,
        "weekly_7d_remaining_credit_usd": 39.0,
    }
    target_windows = as_target["credit_calibration"]["codex"]["shared"]
    assert target_windows["session_5h"]["reserve_credit_usd"] == 1.0
    assert target_windows["weekly_7d"]["reserve_credit_usd"] == 1.0


@pytest.mark.parametrize("host", ["agy", "codex", "claude"])
def test_min_remaining_credit_applies_to_every_window_for_any_root(tmp_path, host):
    _write_claude_catalog(["claude-opus-5-5", "claude-fable-5-2"])
    router = _router(tmp_path, '[agents.claude]\nplan = "max-5x"\nmin_remaining_credit_usd = 10.0\n')
    # max-5x: 90 USD session, 225 USD weekly, and a 75 USD weekly fable tier.
    router.usage_set(
        "claude", session_used_pct=50.0, weekly_used_pct=20.0, tier="fable", tier_used_pct=60.0, now=NOW
    )
    result = router.candidates(host, include_agents=["claude"], now=NOW)

    # Shared windows: 45 - 10 = 35 for the session and 180 - 10 = 170 for the week.
    assert _cell(result, "claude/claude-opus-5-5")["credit_budget"] == {
        "available_credit_usd": 35.0,
        "session_5h_remaining_credit_usd": 35.0,
        "weekly_7d_remaining_credit_usd": 170.0,
    }
    # The fable tier keeps the same amount: 30 - 10 = 20, tighter than the shared week.
    assert _cell(result, "claude/claude-fable-5-2")["credit_budget"] == {
        "available_credit_usd": 20.0,
        "session_5h_remaining_credit_usd": 35.0,
        "weekly_7d_remaining_credit_usd": 20.0,
    }
    for cell in result["candidates"]:
        assert set(cell["credit_budget"]) <= HOWHARD_BUDGET_KEYS

    calibration = result["credit_calibration"]["claude"]
    assert calibration["shared"]["session_5h"]["reserve_credit_usd"] == 10.0
    assert calibration["shared"]["weekly_7d"]["reserve_credit_usd"] == 10.0
    assert calibration["fable"]["weekly_7d"]["remaining_credit_usd"] == 20.0
    assert calibration["fable"]["weekly_7d"]["reserve_credit_usd"] == 10.0
    assert _reserve_warnings(result) == []

    fable = router.usage_show("claude", now=NOW)["agents"]["claude"]["models"]["claude-fable-5-2"]
    assert fable["credit_budget"]["available_credit_usd"] == 20.0
    assert fable["credit_calibration"]["weekly_7d"]["reserve_credit_usd"] == 10.0


def test_window_without_a_credit_ceiling_keeps_percentage_limits_and_warns(tmp_path):
    _write_agy_models_cache(
        [
            {"model": "gemini-4-carbon-high", "effort": "high"},
            {"model": "claude-opus-5-5-high", "effort": "high"},
        ]
    )
    percentage_only = _router(tmp_path, '[agents.agy]\nplan = "free"\n', "percentage.toml")
    # The free plan has gemini credit ceilings (3 USD session, 10 USD weekly) but none for 3p.
    for tier in ("gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly"):
        percentage_only.usage_set("agy", tier=tier, tier_used_pct=50.0, now=NOW)
    assert _reserve_warnings(percentage_only.candidates("codex", include_agents=["agy"], now=NOW)) == []

    router = _router(tmp_path, '[agents.agy]\nplan = "free"\nmin_remaining_credit_usd = 1.0\n', "reserved.toml")
    result = router.candidates("codex", include_agents=["agy"], now=NOW)
    # Gemini windows keep the reserve: 1.5 - 1 = 0.5 for the session and 5 - 1 = 4 for the week.
    assert _cell(result, "agy/gemini-4-carbon-high")["credit_budget"] == {
        "available_credit_usd": 0.5,
        "session_5h_remaining_credit_usd": 0.5,
        "weekly_7d_remaining_credit_usd": 4.0,
    }
    # The uncalibrated 3p windows stay routable under the percentage limits, with a warning.
    assert "credit_budget" not in _cell(result, "agy/claude-opus-5-5-high")
    assert _reserve_warnings(result) == [
        "agy quota_group '3p' session_5h: no known credit ceiling, so the 1 USD credit reserve "
        "is not applied (percentage limits only)",
        "agy quota_group '3p' weekly_7d: no known credit ceiling, so the 1 USD credit reserve "
        "is not applied (percentage limits only)",
    ]

    router.usage_set("agy", tier="3p-5h", tier_used_pct=91.0, now=NOW)
    result = router.candidates("codex", include_agents=["agy"], now=NOW)
    assert _reasons(result, "agy") == ["session_5h used_pct (91.0%) >= effective limit (90.0%)"]
    assert _reserve_warnings(result) == []


def test_cli_candidates_envelope_warns_when_no_window_has_a_credit_ceiling(tmp_path, capsys):
    config = tmp_path / "agroute.toml"
    config.write_text('[agents.codex]\nplan = "none"\nmin_remaining_credit_usd = 5.0\n', encoding="utf-8")
    common = ["--config", str(config), "--state-dir", str(tmp_path / "state"), "--no-user-config", "--now", NOW]
    assert main(
        ["usage", "set", "--agent", "codex", "--session-used-pct", "40", "--weekly-used-pct", "40", *common, "--json"]
    ) == 0
    capsys.readouterr()

    assert main(["candidates", "--host", "agy", "--include-agents", "codex", *common, "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["candidates"]
    assert all("credit_budget" not in cell for cell in envelope["candidates"])
    assert envelope["warnings"] == [
        "codex quota_group 'shared' session_5h: no known credit ceiling, so the 5 USD credit reserve "
        "is not applied (percentage limits only)",
        "codex quota_group 'shared' weekly_7d: no known credit ceiling, so the 5 USD credit reserve "
        "is not applied (percentage limits only)",
    ]


@pytest.mark.parametrize("key", ["host_reserve_credit_usd", "min_remaining_credit_usd"])
@pytest.mark.parametrize(
    "raw,message",
    [
        ("-0.5", "expected a non-negative number"),
        ("inf", "expected a finite number"),
        ("nan", "expected a finite number"),
        ("true", "expected a finite number"),
        ('"10"', "expected a finite number"),
    ],
)
def test_invalid_credit_reserve_is_refused(tmp_path, key, raw, message):
    config = tmp_path / "agroute.toml"
    config.write_text(f"[agents.claude]\n{key} = {raw}\n", encoding="utf-8")
    with pytest.raises(AgRouteError, match=re.escape(f"agents.claude.{key}: {message}")):
        load_config(config)


def test_unknown_credit_reserve_key_is_refused(tmp_path):
    config = tmp_path / "agroute.toml"
    config.write_text("[agents.codex]\nhost_reserve_credit = 5.0\n", encoding="utf-8")
    with pytest.raises(AgRouteError, match=re.escape("agents.codex: unknown keys ['host_reserve_credit']")):
        load_config(config)


def test_cli_reports_credit_reserves_and_refuses_negative_amounts(tmp_path, capsys):
    config = tmp_path / "agroute.toml"
    config.write_text("[agents.codex]\nhost_reserve_credit_usd = 25\nmin_remaining_credit_usd = 10\n", encoding="utf-8")
    common = ["--config", str(config), "--state-dir", str(tmp_path / "state"), "--no-user-config", "--json"]
    assert main(["config", "show", *common]) == 0
    agents = json.loads(capsys.readouterr().out)["agents"]
    assert {name: (cfg["host_reserve_credit_usd"], cfg["min_remaining_credit_usd"]) for name, cfg in agents.items()} == {
        "codex": (25.0, 10.0),
        "claude": (0.0, 0.0),
        "agy": (0.0, 0.0),
    }

    config.write_text("[agents.codex]\nmin_remaining_credit_usd = -10.0\n", encoding="utf-8")
    assert main(["config", "show", *common]) == 1
    assert "agents.codex.min_remaining_credit_usd: expected a non-negative number" in capsys.readouterr().err

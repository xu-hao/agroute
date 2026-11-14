"""Behavioral coverage for subscription cost, list-price credit, and backend routing."""

import json
from pathlib import Path

import pytest

from agroute import Router, load_config
from agroute.calibration import calibration_path, read_calibration_store, write_calibration_store

NOW = "2026-11-12T12:00:00Z"


def _write_codex_cache(root: Path, model: str = "gpt-6.1-sol") -> None:
    (root / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": model,
                        "visibility": "list",
                        "priority": 1,
                        "supported_reasoning_levels": [{"effort": "high"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def test_plan_aliases_separate_monthly_cost_from_credit_ceilings(tmp_path):
    config_path = tmp_path / "agroute.toml"
    config_path.write_text(
        '[agents.codex]\nplan = "promax"\n\n'
        '[agents.agy]\nplan = "plus-8"\n\n'
        '[agents.claude]\nplan = "max-5x"\n',
        encoding="utf-8",
    )

    cfg = load_config(config_path, include_user_config=False)

    assert cfg.agents["codex"]["plan"] == "pro-500"
    assert cfg.agents["codex"]["monthly_cost_usd"] == 500.0
    assert cfg.agents["codex"]["credit"] == {
        "session_5h_credit_usd": 500.0,
        "weekly_7d_credit_usd": 1250.0,
        "tiers": {},
    }
    assert cfg.agents["agy"]["monthly_cost_usd"] == 8.0
    assert cfg.agents["claude"]["monthly_cost_usd"] == 100.0




def test_legacy_credit_config_aliases_normalize_to_explicit_credit_names(tmp_path):
    config_path = tmp_path / "agroute.toml"
    config_path.write_text(
        '[agents.claude]\nplan = "pro-20"\n\n'
        '[agents.claude.credit]\nsession_5h_usd = 7.0\n\n'
        '[agents.agy]\nplan = "pro-20"\n\n'
        '[agents.agy.credit.tiers.gemini]\nweekly_7d_usd = 140.0\n',
        encoding="utf-8",
    )

    cfg = load_config(config_path, include_user_config=False)

    assert cfg.agents["claude"]["credit"]["session_5h_credit_usd"] == 7.0
    assert cfg.agents["agy"]["credit"]["tiers"]["gemini"]["weekly_7d_credit_usd"] == 140.0


def test_v1_calibration_store_migrates_cost_statistic_to_credit(tmp_path):
    config_path = tmp_path / "agroute.toml"
    config_path.write_text('[agents.codex]\nplan = "plus-20"\n', encoding="utf-8")
    state_dir = tmp_path / "state"
    router = Router(config_path, project=tmp_path, state_directory=state_dir, include_user_config=False)
    path = calibration_path(router.config)
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy_store = {
        "format": "agroute-credit-calibration-v1",
        "pools": {
            "codex:plus-20:shared:weekly_7d": {
                "agent": "codex",
                "plan": "plus-20",
                "quota_group": "shared",
                "window": "weekly_7d",
                "s_cost": 8.5,
                "s_frac": 0.25,
                "matched_intervals": 1,
            }
        },
    }
    path.write_text(json.dumps(legacy_store), encoding="utf-8")

    migrated = read_calibration_store(router.config)
    assert migrated["format"] == "agroute-credit-calibration-v2"
    assert migrated["pools"]["codex:plus-20:shared:weekly_7d"]["s_credit"] == 8.5
    assert "s_cost" not in migrated["pools"]["codex:plus-20:shared:weekly_7d"]

    write_calibration_store(router.config, migrated)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["format"] == "agroute-credit-calibration-v2"
    assert written["pools"]["codex:plus-20:shared:weekly_7d"]["s_credit"] == 8.5

def test_monthly_cost_and_nested_credit_overrides_validate_independently(tmp_path):
    config_path = tmp_path / "agroute.toml"
    config_path.write_text(
        '[agents.agy]\nplan = "pro-20"\nmonthly_cost_usd = 24.0\n\n'
        "[agents.agy.credit.tiers.gemini]\n"
        "weekly_7d_credit_usd = 150.0\n\n"
        "[agents.agy.credit.tiers.3p]\n"
        "session_5h_credit_usd = 6.0\n",
        encoding="utf-8",
    )

    cfg = load_config(config_path, include_user_config=False)

    assert cfg.agents["agy"]["monthly_cost_usd"] == 24.0
    assert cfg.agents["agy"]["weekly_cost_usd"] == pytest.approx(24.0 * 7.0 / 30.0)
    assert cfg.agents["agy"]["credit"] == {
        "tiers": {
            "gemini": {"session_5h_credit_usd": 32.0, "weekly_7d_credit_usd": 150.0},
            "3p": {"session_5h_credit_usd": 6.0, "weekly_7d_credit_usd": 10.0},
        }
    }


@pytest.mark.parametrize(
    "agent,plan,canonical,monthly_cost",
    [
        ("agy", "free", "free", 0.0),
        ("agy", "plus-8", "plus-8", 8.0),
        ("agy", "pro-20", "pro-20", 20.0),
        ("agy", "ultra-100", "ultra-100", 100.0),
        ("agy", "ultra-200", "ultra-200", 200.0),
        ("claude", "pro-20", "pro-20", 20.0),
        ("claude", "max-5x", "max-5x", 100.0),
        ("claude", "max-20x", "max-20x", 200.0),
        ("codex", "plus-20", "plus-20", 20.0),
        ("codex", "pro-200", "pro-200", 200.0),
        ("codex", "pro-500", "pro-500", 500.0),
        ("codex", "pro", "pro-200", 200.0),
        ("codex", "chatgpt-pro", "pro-200", 200.0),
    ],
)
def test_packaged_plan_monthly_costs(tmp_path, agent, plan, canonical, monthly_cost):
    config_path = tmp_path / "agroute.toml"
    config_path.write_text(f'[agents.{agent}]\nplan = "{plan}"\n', encoding="utf-8")

    cfg = load_config(config_path, include_user_config=False)

    assert cfg.agents[agent]["plan"] == canonical
    assert cfg.agents[agent]["monthly_cost_usd"] == monthly_cost
    assert cfg.agents[agent]["weekly_cost_usd"] == pytest.approx(monthly_cost * 7.0 / 30.0)


def test_candidate_cost_per_credit_uses_free_payg_and_proportional_pool_costs(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    _write_codex_cache(codex_home)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    agy_config = tmp_path / "agy.toml"
    agy_config.write_text(
        '[agents.agy]\nplan = "pro-20"\n\n'
        '[[agents.agy.native_candidates]]\nmodel = "gemini-4-carbon-high"\neffort = "high"\n\n'
        '[[agents.agy.native_candidates]]\nmodel = "claude-opus-5-5-high"\neffort = "high"\n',
        encoding="utf-8",
    )
    agy = Router(agy_config, project=tmp_path, include_user_config=False)
    result = agy.candidates("agy", include_agents=["agy"], now=NOW)
    cells = {cell["model"]: cell for cell in result["candidates"]}
    expected = (20.0 * 7.0 / 30.0) / (135.0 + 10.0)
    assert cells["agy/gemini-4-carbon-high"]["cost_per_credit"] == pytest.approx(expected)
    assert cells["agy/claude-opus-5-5-high"]["cost_per_credit"] == pytest.approx(expected)

    pools = result["credit_calibration"]["agy"]
    gemini_cost = pools["gemini"]["weekly_7d"]["weekly_cost_usd"]
    third_party_cost = pools["3p"]["weekly_7d"]["weekly_cost_usd"]
    assert gemini_cost + third_party_cost == pytest.approx(20.0 * 7.0 / 30.0)
    assert gemini_cost / 135.0 == pytest.approx(expected)
    assert third_party_cost / 10.0 == pytest.approx(expected)
    usage = agy.usage_show("agy", now=NOW)["agents"]["agy"]
    assert usage["monthly_cost_usd"] == 20.0
    assert usage["weekly_cost_usd"] == pytest.approx(20.0 * 7.0 / 30.0)
    assert usage["models"]["gemini-4-carbon-high"]["cost_per_credit"] == pytest.approx(expected)

    free_config = tmp_path / "free.toml"
    free_config.write_text(
        '[agents.agy]\nplan = "free"\n\n'
        '[[agents.agy.native_candidates]]\nmodel = "gemini-4-carbon-high"\neffort = "high"\n',
        encoding="utf-8",
    )
    free_router = Router(free_config, project=tmp_path, include_user_config=False)
    free_cell = free_router.candidates("agy", include_agents=["agy"], now=NOW)["candidates"][0]
    assert free_cell["cost_per_credit"] == 0.0

    payg_config = tmp_path / "payg.toml"
    payg_config.write_text(
        '[agents.codex]\nplan = "none"\n\n'
        '[[agents.codex.cli_candidates]]\nmodel = "gpt-6.1-sol"\neffort = "high"\n',
        encoding="utf-8",
    )
    payg_router = Router(payg_config, project=tmp_path, include_user_config=False)
    payg_cell = payg_router.candidates("agy", include_agents=["codex"], now=NOW)["candidates"][0]
    assert payg_cell["cost_per_credit"] == 1.0


class _FixtureLLM:
    identity = "cost-credit-routing-fixture"

    def complete(self, request):
        if request["kind"] == "filter":
            cells = sorted(request["cells"], key=lambda cell: (cell["model"], cell["effort"]))
            order = [cell["key"] for cell in cells]
            return {
                "filter": {"order": order, "lower": order[0], "upper": order[-1]},
                "memory_filters": [],
                "reason": "synthetic integration inventory",
            }
        if request["kind"] == "learn":
            return {"operations": [], "reason": "no memory updates"}
        if request["phase"] == "baseline":
            return {
                "forecasts": [
                    {
                        "family": cell["family"],
                        "effort": cell["effort"],
                        "clean": 0.98,
                        "loud": 0.01,
                        "silent": 0.01,
                        "tokens": 10_000.0,
                        "seconds": 10.0,
                    }
                    for cell in request["cells"]
                ],
                "reason": "identical synthetic forecasts",
            }
        return {"memory_forecasts": [], "reason": "no memories"}


def test_howhard_v7_ranks_by_session_cost_and_plan_checks_credit_budget(tmp_path, monkeypatch):
    howhard = pytest.importorskip("howhard")
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    _write_codex_cache(codex_home)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    config_path = tmp_path / "agroute.toml"
    config_path.write_text(
        '[agents.codex]\nplan = "pro-200"\n\n'
        '[[agents.codex.cli_candidates]]\nmodel = "gpt-6.1-sol"\neffort = "high"\nusd_per_mtok = 200.0\n\n'
        '[agents.claude]\nplan = "pro-20"\n\n'
        '[[agents.claude.cli_candidates]]\nmodel = "claude-opus-5-5"\neffort = "high"\nusd_per_mtok = 10.0\n',
        encoding="utf-8",
    )
    router = Router(config_path, project=tmp_path, include_user_config=False)
    router.usage_set("codex", session_used_pct=0.0, weekly_used_pct=0.0, now=NOW)
    router.usage_set("claude", session_used_pct=0.0, weekly_used_pct=0.0, now=NOW)
    emitted = router.candidates("agy", include_agents=["codex", "claude"], now=NOW)["candidates"]
    assert all("cost_per_credit" in cell for cell in emitted)
    assert all(
        set(cell["credit_budget"])
        == {"available_credit_usd", "session_5h_remaining_credit_usd", "weekly_7d_remaining_credit_usd"}
        for cell in emitted
    )

    engine = howhard.Engine(directory=tmp_path / "howhard", llm=_FixtureLLM())
    pending = engine.prepare("task", "Compare available routes", risk="routine", native_candidates=emitted)
    from howhard.capability import EFFORT_ORDER

    cells = sorted(
        pending["baseline_request"]["cells"],
        key=lambda cell: (cell["model"], EFFORT_ORDER.get(cell["effort"], 99)),
    )
    keys = [cell["key"] for cell in cells]
    pending = engine.decide(
        pending["request_id"],
        {
            "filter": {"order": keys, "lower": keys[0], "upper": keys[-1]},
            "memory_filters": [],
            "reason": "all emitted routes are in scope",
        },
    )
    decision = engine.decide(
        pending["request_id"],
        {
            "forecasts": [
                {
                    "family": cell["family"],
                    "effort": cell["effort"],
                    "clean": 0.98,
                    "loud": 0.01,
                    "silent": 0.01,
                    "tokens": 10_000.0,
                    "seconds": 10.0,
                }
                for cell in pending["baseline_request"]["cells"]
            ],
            "memory_forecasts": [],
            "reason": "identical synthetic forecasts",
        },
        seed=7,
    )

    from howhard.decisions import load as load_howhard_decision

    receipt = load_howhard_decision(engine.config, decision["decision_id"])
    assert receipt["format"] == "howhard-decision-v7"
    by_model = {cell["model"]: cell for cell in decision["cells"]}
    assert by_model["codex/gpt-6.1-sol"]["needed_credit_usd"] > by_model["claude/claude-opus-5-5"]["needed_credit_usd"]
    assert by_model["codex/gpt-6.1-sol"]["session_cost_usd"] > by_model["claude/claude-opus-5-5"]["session_cost_usd"]
    assert all(cell["credit_ok"] for cell in decision["cells"])
    assert decision["chosen"]["model"] == "claude/claude-opus-5-5"

    plan = router.plan("agy", decision, prompt="select the cheapest valid route", now=NOW)
    assert plan["needed_credit_usd"] == pytest.approx(by_model["claude/claude-opus-5-5"]["needed_credit_usd"])
    assert plan["credit_budget"]["available_credit_usd"] >= plan["needed_credit_usd"]
    assert plan["session_cost_usd"] == pytest.approx(by_model["claude/claude-opus-5-5"]["session_cost_usd"])

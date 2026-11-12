"""Antigravity plan changes: complete `/usage` reports replace the tier set (#35)."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from agroute import Router
from agroute.cli import main

T0 = "2026-11-12T10:00:00Z"
T1 = "2026-11-12T11:00:00Z"  # beyond the 1800 s freshness window after T0
T1_LATER = "2026-11-12T11:01:00Z"
WEEK_RESET = "2026-11-19T04:01:45Z"
SESSION_RESET = "2026-11-12T14:00:00Z"


def bucket(bucket_id, window, remaining, reset):
    return {"id": bucket_id, "name": f"{window} limit", "window": window,
            "remaining_fraction": remaining, "reset_time": reset}


def gemini_group(weekly_remaining=0.96, session_remaining=0.94):
    return {"name": "Gemini Models", "description": "Models within this group: Gemini Flash, Gemini Pro",
            "buckets": [bucket("gemini-weekly", "weekly", weekly_remaining, WEEK_RESET),
                        bucket("gemini-5h", "5h", session_remaining, SESSION_RESET)]}


def third_party_group():
    return {"name": "Claude and GPT models", "description": "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS",
            "buckets": [bucket("3p-weekly", "weekly", 0.57, WEEK_RESET),
                        bucket("3p-5h", "5h", 0.55, SESSION_RESET)]}


def argon_group():
    return {"name": "Gemini 4 Argon", "buckets": [bucket("gemini-argon-wtu-weekly", "weekly", 0.40, WEEK_RESET)]}


def usage_report(groups, status="SUCCESS"):
    return json.dumps({"conversation_id": "", "status": status, "response": "",
                       "command": {"name": "usage", "data": {"description": "plan quotas", "groups": groups}}})


OLD_PLAN = usage_report([gemini_group(), third_party_group(), argon_group()])
NEW_PLAN = usage_report([gemini_group(), third_party_group()])
OLD_ONLY_TIERS = {"argon", "argon-weekly", "gemini-argon-wtu-weekly"}


@pytest.fixture
def plan_router(tmp_path):
    """A router whose agy probe command prints an owned, swappable `/usage` report."""
    report = tmp_path / "agy-usage.json"
    script = tmp_path / "agy-usage-probe.py"
    script.write_text(f"import sys\nsys.stdout.write(open({str(report)!r}).read())\n", encoding="utf-8")
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        "[agents.agy]\n"
        f"usage_probe_cmd = {json.dumps([sys.executable, str(script)])}\n"
        "cli_candidates = [\n"
        '  { model = "gemini-4-argon-high", effort = "high" },\n'
        '  { model = "gemini-3.8-flash-low", effort = "low" },\n'
        '  { model = "claude-opus-5-5-medium", effort = "medium" },\n'
        "]\n",
        encoding="utf-8",
    )

    def probe(text, now):
        report.write_text(text, encoding="utf-8")
        return router.usage_probe("agy", now=now)

    router = Router(cfg)  # the working directory, as the CLI sees it
    router.probe_with = probe
    router.config_path = cfg
    return router


def agy_tiers(router, now):
    return set(router.usage_show("agy", now=now)["agents"]["agy"]["tiers"])


def test_complete_report_after_plan_change_drops_old_plan_tiers(plan_router):
    plan_router.probe_with(OLD_PLAN, T0)
    assert OLD_ONLY_TIERS <= agy_tiers(plan_router, T0)

    plan_router.probe_with(NEW_PLAN, T1)

    tiers = agy_tiers(plan_router, T1)
    assert tiers.isdisjoint(OLD_ONLY_TIERS)
    assert {"gemini-weekly", "gemini-5h", "3p-weekly", "3p-5h"} <= tiers
    cells = {c["model"] for c in plan_router.candidates("codex", now=T1_LATER)["candidates"]}
    assert {"agy/gemini-4-argon-high", "agy/gemini-3.8-flash-low", "agy/claude-opus-5-5-medium"} <= cells


def test_partial_report_keeps_tiers_it_does_not_cover(plan_router):
    plan_router.probe_with(OLD_PLAN, T0)
    unreported = third_party_group()
    unreported["buckets"][0] = {"id": "3p-weekly", "window": "weekly"}  # no quota evidence
    partial = usage_report([gemini_group(weekly_remaining=0.90), unreported])

    plan_router.probe_with(partial, "2026-11-12T10:05:00Z")

    state = plan_router.usage_show("agy", now="2026-11-12T10:06:00Z")["agents"]["agy"]
    assert OLD_ONLY_TIERS | {"3p-weekly"} <= set(state["tiers"])
    assert state["tiers"]["3p-weekly"]["used_pct"] == pytest.approx(43.0)
    assert state["tiers"]["gemini-weekly"]["used_pct"] == pytest.approx(10.0)


def test_unsuccessful_report_does_not_replace_tier_set(plan_router):
    plan_router.probe_with(OLD_PLAN, T0)

    plan_router.probe_with(usage_report([gemini_group()], status="ERROR"), "2026-11-12T10:05:00Z")

    assert OLD_ONLY_TIERS | {"3p-weekly", "3p-5h"} <= agy_tiers(plan_router, "2026-11-12T10:06:00Z")


def test_argon_models_use_combined_gemini_quota_without_argon_bucket(plan_router):
    plan_router.probe_with(OLD_PLAN, T0)
    plan_router.probe_with(usage_report([gemini_group(weekly_remaining=0.05), third_party_group()]), T1)

    models = plan_router.usage_show("agy", now=T1)["agents"]["agy"]["models"]
    argon = models["gemini-4-argon-high"]
    assert argon["quota_group"] == "gemini"
    assert argon["weekly_7d"]["used_pct"] == pytest.approx(95.0)
    assert argon["session_5h"]["used_pct"] == pytest.approx(6.0)
    assert argon["eligible"] is False
    assert models["claude-opus-5-5-medium"]["eligible"] is True
    cells = {c["model"] for c in plan_router.candidates("codex", now=T1_LATER)["candidates"]}
    assert "agy/gemini-4-argon-high" not in cells


def _xdg_cache(monkeypatch):
    """Use the default `$XDG_DATA_HOME/agroute` shared cache location."""
    spool = Path(os.environ["AGROUTE_SPOOL_DIR"])
    data = Path(os.environ["XDG_DATA_HOME"]) / "agroute"
    data.mkdir(parents=True, exist_ok=True)
    shutil.copy(spool / "agy_models_cache.json", data / "agy_models_cache.json")
    monkeypatch.delenv("AGROUTE_SPOOL_DIR")
    return data / "live_usage_cache.json"


def _cli(router, *args):
    return main([*args, "--config", str(router.config_path), "--json"])


def test_usage_clear_agent_removes_its_shared_cache_entry(plan_router, monkeypatch, capsys):
    cache = _xdg_cache(monkeypatch)
    plan_router.usage_ingest("codex", json.dumps({"rate_limits": {"primary": {"used_percent": 5, "window_minutes": 300}}}), now=T0)
    plan_router.probe_with(OLD_PLAN, T0)
    assert set(json.loads(cache.read_text())["agents"]) == {"agy", "codex"}

    assert _cli(plan_router, "usage", "clear", "--agent", "agy") == 0
    capsys.readouterr()

    assert set(json.loads(cache.read_text())["agents"]) == {"codex"}
    assert agy_tiers(plan_router, T0) == set()


def test_full_usage_clear_removes_every_shared_cache_entry(plan_router, monkeypatch, capsys):
    cache = _xdg_cache(monkeypatch)
    plan_router.usage_ingest("codex", json.dumps({"rate_limits": {"primary": {"used_percent": 5, "window_minutes": 300}}}), now=T0)
    plan_router.probe_with(OLD_PLAN, T0)

    assert _cli(plan_router, "usage", "clear") == 0
    capsys.readouterr()

    assert json.loads(cache.read_text())["agents"] == {}
    assert agy_tiers(plan_router, T0) == set()

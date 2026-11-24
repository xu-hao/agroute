"""Behavioral tests for issue #66: headroom-scaled freshness, in-flight reservations, and host 429 ingestion."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agroute import AgRouteError, Router
from agroute.adapters import get_adapter
from agroute.cli import main as cli_main

T0 = "2026-11-21T10:00:00Z"
T_30S = "2026-11-21T10:00:30Z"
T_2M = "2026-11-21T10:02:00Z"
T_10M = "2026-11-21T10:10:00Z"
T_35M = "2026-11-21T10:35:00Z"
RESET_5H = "2026-11-21T15:00:00Z"
RESET_7D = "2026-11-25T00:00:00Z"


def _write_claude_config(tmp_path: Path, extra_claude: str = "") -> Path:
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        "[agents.claude]\n"
        'plan = "max-5x"\n'
        "native_candidates = [\n"
        '  { model = "claude-fable-5-2", effort = "high", usd_per_mtok = 10.0 },\n'
        '  { model = "claude-opus-5-5", effort = "high", usd_per_mtok = 10.0 },\n'
        '  { model = "claude-sonnet-5-5", effort = "medium", usd_per_mtok = 5.0 },\n'
        "]\n"
        "cli_candidates = [\n"
        '  { model = "claude-fable-5-2", effort = "high", usd_per_mtok = 10.0 },\n'
        '  { model = "claude-opus-5-5", effort = "high", usd_per_mtok = 10.0 },\n'
        '  { model = "claude-sonnet-5-5", effort = "medium", usd_per_mtok = 5.0 },\n'
        "]\n"
        f"{extra_claude}",
        encoding="utf-8",
    )
    return cfg


def _patch_probe(monkeypatch, agent: str, patches: list):
    calls = []

    def _probe(cwd):
        calls.append(cwd)
        val = patches[min(len(calls) - 1, len(patches) - 1)]
        if isinstance(val, Exception):
            raise val
        return val

    monkeypatch.setattr(get_adapter(agent), "probe_usage", _probe)
    return calls


def _fable_decision(decision_id: str, needed_credit_usd: float) -> dict:
    return {
        "format": "howhard-decision-v9",
        "decision_id": decision_id,
        "chosen": {"model": "claude/claude-fable-5-2", "effort": "high"},
        "selection": {
            "cap_met": True,
            "needs_review": False,
            "cells": [
                {
                    "model": "claude/claude-fable-5-2",
                    "effort": "high",
                    "J": 2.0,
                    "needed_credit_usd": needed_credit_usd,
                    "session_cost_usd": needed_credit_usd * 0.2,
                    "cost_per_credit": 0.2,
                    "admissible": True,
                }
            ],
        },
    }


def test_low_headroom_tier_reprobes_before_filtering_without_double_probing(tmp_path, monkeypatch):
    cfg = _write_claude_config(tmp_path)
    router = Router(cfg, project=tmp_path)

    # 1. Ample headroom (50% used): at T0 + 10m (600s <= 1800s), no probe is triggered.
    router.usage_set("claude", session_used_pct=20.0, session_reset_at=RESET_5H, weekly_used_pct=20.0, weekly_reset_at=RESET_7D, now=T0)
    router.usage_set("claude", tier="fable-weekly", tier_used_pct=50.0, tier_reset_at=RESET_7D, now=T0)
    calls = _patch_probe(monkeypatch, "claude", [AgRouteError("should not probe at ample headroom")])
    res_ample = router.candidates("claude", include_agents=["claude"], now=T_10M)
    assert not calls
    assert any(c["model"] == "claude/claude-fable-5-2" for c in res_ample["candidates"])

    # 2. Low headroom on fable-weekly (78% used vs 95% max_tier_used_pct -> 17% < 20% threshold):
    # At T0 + 30s (<= 60s floor), candidates -> plan does NOT double-probe.
    router.usage_set("claude", session_used_pct=20.0, session_reset_at=RESET_5H, weekly_used_pct=20.0, weekly_reset_at=RESET_7D, now=T0)
    router.usage_set("claude", tier="fable-weekly", tier_used_pct=78.0, tier_reset_at=RESET_7D, now=T0)
    calls_30s = _patch_probe(monkeypatch, "claude", [AgRouteError("should not probe within floor")])
    res_30s = router.candidates("claude", include_agents=["claude"], now=T_30S)
    assert not calls_30s
    assert any(c["model"] == "claude/claude-fable-5-2" for c in res_30s["candidates"])

    # 3. At T0 + 2m (> 60s floor), low headroom forces a fresh re-probe before filtering.
    # The fresh probe reports fable-weekly at 100%, so fable is excluded while opus remains eligible.
    calls_2m = _patch_probe(
        monkeypatch,
        "claude",
        [
            {
                "session_5h": {"used_pct": 25.0, "reset_at": RESET_5H},
                "weekly_7d": {"used_pct": 25.0, "reset_at": RESET_7D},
                "tiers": {"fable-weekly": {"used_pct": 100.0, "reset_at": RESET_7D}},
            }
        ],
    )
    res_2m = router.candidates("claude", include_agents=["claude"], now=T_2M)
    assert len(calls_2m) == 1
    models_2m = {c["model"] for c in res_2m["candidates"]}
    assert "claude/claude-fable-5-2" not in models_2m
    assert "claude/claude-opus-5-5" in models_2m

    # 4. Already-exhausted tier (100% >= 95%) uses normal staleness bound, not the 60s floor.
    calls_exhausted = _patch_probe(monkeypatch, "claude", [AgRouteError("should not re-probe exhausted tier at +2m")])
    res_after = router.candidates("claude", include_agents=["claude"], now="2026-11-21T10:04:00Z")
    assert not calls_exhausted
    assert "claude/claude-fable-5-2" not in {c["model"] for c in res_after["candidates"]}


def test_low_headroom_config_validation(tmp_path):
    good = tmp_path / "good.toml"
    good.write_text(
        "[agents.claude]\n"
        "usage_low_headroom_pct = 15.0\n"
        "usage_low_headroom_stale_after_seconds = 30\n",
        encoding="utf-8",
    )
    router = Router(good, project=tmp_path)
    assert router.config.agents["claude"]["usage_low_headroom_pct"] == 15.0
    assert router.config.agents["claude"]["usage_low_headroom_stale_after_seconds"] == 30.0

    bad_pct = tmp_path / "bad_pct.toml"
    bad_pct.write_text("[agents.claude]\nusage_low_headroom_pct = 150.0\n", encoding="utf-8")
    with pytest.raises(AgRouteError, match="usage_low_headroom_pct"):
        Router(bad_pct, project=tmp_path)

    bad_secs = tmp_path / "bad_secs.toml"
    bad_secs.write_text("[agents.claude]\nusage_low_headroom_stale_after_seconds = -5\n", encoding="utf-8")
    with pytest.raises(AgRouteError, match="usage_low_headroom_stale_after_seconds"):
        Router(bad_secs, project=tmp_path)


def test_two_launches_exceeding_remaining_credit_are_not_both_admitted(tmp_path):
    cfg = _write_claude_config(tmp_path)
    router = Router(cfg, project=tmp_path)
    # claude max-5x: shared 5h = $90 (10% base rsv = $9), shared 7d = $225 (5% base rsv = $11.25), fable 7d = $75 (5% base rsv = $3.75).
    # At 10% shared, 75% fable -> fable remaining = $15.0, shared 5h = $72.0, shared 7d = $191.25.
    router.usage_set("claude", session_used_pct=10.0, session_reset_at=RESET_5H, weekly_used_pct=10.0, weekly_reset_at=RESET_7D, now=T0)
    router.usage_set("claude", tier="fable-weekly", tier_used_pct=75.0, tier_reset_at=RESET_7D, now=T0)

    # First launch reserves $10 of the $15 remaining credit.
    plan1 = router.plan("claude", _fable_decision("dec-launch-1", 10.0), prompt="worker 1", include_agents=["claude"], now=T0)
    assert plan1["credit_budget"]["available_credit_usd"] == 15.0
    assert plan1["needed_credit_usd"] == 10.0

    # Candidates now reflect the $10 in-flight reservation across both fable and overlapping shared pools.
    cands = router.candidates("claude", include_agents=["claude"], now=T0)
    by_model = {c["model"]: c for c in cands["candidates"]}
    assert by_model["claude/claude-fable-5-2"]["credit_budget"]["available_credit_usd"] == 5.0
    assert by_model["claude/claude-opus-5-5"]["credit_budget"]["session_5h_remaining_credit_usd"] == 62.0
    assert by_model["claude/claude-opus-5-5"]["credit_budget"]["weekly_7d_remaining_credit_usd"] == 181.25

    # Second launch needing $10 exceeds the $5 remaining after reservation and is refused.
    with pytest.raises(AgRouteError, match=r"exceeding remaining credit 5 USD.*reserved"):
        router.plan("claude", _fable_decision("dec-launch-2", 10.0), prompt="worker 2", include_agents=["claude"], now=T0)

    # A smaller launch ($4 <= $5) is still admitted, leaving $1.
    plan3 = router.plan("claude", _fable_decision("dec-launch-3", 4.0), prompt="worker 3", include_agents=["claude"], now=T0)
    assert plan3["credit_budget"]["available_credit_usd"] == 5.0
    cands_after = router.candidates("claude", include_agents=["claude"], now=T0)
    by_model_after = {c["model"]: c for c in cands_after["candidates"]}
    assert by_model_after["claude/claude-fable-5-2"]["credit_budget"]["available_credit_usd"] == 1.0


def test_reservation_updated_by_measured_observation_and_released_by_probe_or_clear(tmp_path, monkeypatch):
    cfg = _write_claude_config(tmp_path)
    router = Router(cfg, project=tmp_path)
    router.usage_set("claude", session_used_pct=10.0, session_reset_at=RESET_5H, weekly_used_pct=10.0, weekly_reset_at=RESET_7D, now=T0)
    router.usage_set("claude", tier="fable-weekly", tier_used_pct=75.0, tier_reset_at=RESET_7D, now=T0)

    router.plan("claude", _fable_decision("dec-obs-1", 10.0), prompt="worker 1", include_agents=["claude"], now=T0)
    cands_before = {c["model"]: c for c in router.candidates("claude", include_agents=["claude"], now=T0)["candidates"]}
    assert cands_before["claude/claude-fable-5-2"]["credit_budget"]["available_credit_usd"] == 5.0

    # Recorded howhard observation with measured $3.0 replaces forecast $10.0 until the next probe.
    hh_dir = tmp_path / ".howhard"
    hh_dir.mkdir(parents=True, exist_ok=True)
    obs = {
        "id": "obs-1",
        "decision_id": "dec-obs-1",
        "model": "claude/claude-fable-5-2",
        "effort": "high",
        "outcome": "clean",
        "needed_credit_usd": 3.0,
        "observed_at": T_30S,
    }
    (hh_dir / "pending.jsonl").write_text(json.dumps(obs) + "\n", encoding="utf-8")

    cands_obs = {c["model"]: c for c in router.candidates("claude", include_agents=["claude"], now=T_30S)["candidates"]}
    assert cands_obs["claude/claude-fable-5-2"]["credit_budget"]["available_credit_usd"] == 12.0

    # A fresh probe for the tier releases the reservation completely.
    _patch_probe(
        monkeypatch,
        "claude",
        [
            {
                "session_5h": {"used_pct": 14.0, "reset_at": RESET_5H},
                "weekly_7d": {"used_pct": 12.0, "reset_at": RESET_7D},
                "tiers": {"fable-weekly": {"used_pct": 79.0, "reset_at": RESET_7D}},
            }
        ],
    )
    router.usage_probe("claude", now=T_2M)
    cands_probed = {c["model"]: c for c in router.candidates("claude", include_agents=["claude"], now=T_2M)["candidates"]}
    # (95% - 79%) * $75 = 16% * $75 = $12.0 with zero leftover reservation.
    assert cands_probed["claude/claude-fable-5-2"]["credit_budget"]["available_credit_usd"] == 12.0

    # usage_clear also drops active reservations cleanly.
    router.plan("claude", _fable_decision("dec-obs-2", 6.0), prompt="worker 2", include_agents=["claude"], now=T_2M)
    cands_res = {c["model"]: c for c in router.candidates("claude", include_agents=["claude"], now=T_2M)["candidates"]}
    assert cands_res["claude/claude-fable-5-2"]["credit_budget"]["available_credit_usd"] == 6.0
    router.usage_clear("claude")
    router.usage_set("claude", session_used_pct=14.0, session_reset_at=RESET_5H, weekly_used_pct=12.0, weekly_reset_at=RESET_7D, now=T_2M)
    router.usage_set("claude", tier="fable-weekly", tier_used_pct=79.0, tier_reset_at=RESET_7D, now=T_2M)
    cands_cleared = {c["model"]: c for c in router.candidates("claude", include_agents=["claude"], now=T_2M)["candidates"]}
    assert cands_cleared["claude/claude-fable-5-2"]["credit_budget"]["available_credit_usd"] == 12.0


def test_tier_429_via_usage_set_excludes_tier_until_reset_or_probe_headroom(tmp_path, monkeypatch):
    cfg = _write_claude_config(tmp_path)
    router = Router(cfg, project=tmp_path)
    router.usage_set("claude", session_used_pct=20.0, session_reset_at=RESET_5H, weekly_used_pct=20.0, weekly_reset_at=RESET_7D, now=T0)
    router.usage_set("claude", tier="fable-weekly", tier_used_pct=78.0, tier_reset_at=RESET_7D, now=T0)

    # Report a tier-scoped 429 on fable-weekly via usage_set(--tier ... --rate-limited --cooldown-until ...).
    router.usage_set("claude", tier="fable-weekly", rate_limited=True, cooldown_until=RESET_7D, now=T0)

    res = router.candidates("claude", include_agents=["claude"], now=T0)
    models = {c["model"] for c in res["candidates"]}
    assert "claude/claude-fable-5-2" not in models
    assert "claude/claude-opus-5-5" in models
    assert "claude/claude-sonnet-5-5" in models

    # After 35m (past 1800s), a partial probe that refreshes session_5h + weekly_7d without mentioning
    # fable-weekly must keep claude eligible for opus/sonnet while keeping fable-weekly rejected.
    _patch_probe(
        monkeypatch,
        "claude",
        [
            {
                "session_5h": {"used_pct": 25.0, "reset_at": RESET_5H},
                "weekly_7d": {"used_pct": 25.0, "reset_at": RESET_7D},
            }
        ],
    )
    res_later = router.candidates("claude", include_agents=["claude"], now=T_35M)
    models_later = {c["model"] for c in res_later["candidates"]}
    assert "claude/claude-fable-5-2" not in models_later
    assert "claude/claude-opus-5-5" in models_later

    # Once a probe reports headroom on fable-weekly, fable becomes eligible again.
    _patch_probe(
        monkeypatch,
        "claude",
        [
            {
                "session_5h": {"used_pct": 25.0, "reset_at": RESET_5H},
                "weekly_7d": {"used_pct": 25.0, "reset_at": RESET_7D},
                "tiers": {"fable-weekly": {"used_pct": 40.0, "reset_at": RESET_7D}},
            }
        ],
    )
    router.usage_probe("claude", now=T_35M)
    res_recovered = router.candidates("claude", include_agents=["claude"], now=T_35M)
    assert "claude/claude-fable-5-2" in {c["model"] for c in res_recovered["candidates"]}

    # Re-reject and verify expiry at RESET_7D restores eligibility after a fresh probe.
    router.usage_set("claude", tier="fable-weekly", rate_limited=True, cooldown_until=RESET_7D, now=T_35M)
    assert "claude/claude-fable-5-2" not in {
        c["model"] for c in router.candidates("claude", include_agents=["claude"], now=T_35M)["candidates"]
    }
    _patch_probe(
        monkeypatch,
        "claude",
        [
            {
                "session_5h": {"used_pct": 10.0, "reset_at": "2026-11-25T05:00:00Z"},
                "weekly_7d": {"used_pct": 10.0, "reset_at": "2026-12-02T00:00:00Z"},
            }
        ],
    )
    res_post_reset = router.candidates("claude", include_agents=["claude"], now=RESET_7D)
    assert "claude/claude-fable-5-2" in {c["model"] for c in res_post_reset["candidates"]}


def test_usage_ingest_429_text_cli_tier_scoped_and_agent_wide(tmp_path, capsys):
    cfg = _write_claude_config(tmp_path)
    state_dir = tmp_path / "state"
    router = Router(cfg, project=tmp_path, state_directory=state_dir)
    router.usage_set("claude", session_used_pct=20.0, session_reset_at=RESET_5H, weekly_used_pct=20.0, weekly_reset_at=RESET_7D, now=T0)

    # 1. CLI `agr usage ingest --agent claude --tier fable-weekly --text "..."` blocks only fable.
    rc = cli_main(
        [
            "usage",
            "ingest",
            "--agent",
            "claude",
            "--tier",
            "fable-weekly",
            "--text",
            f"HTTP 429: You've hit your weekly limit · resets {RESET_7D}",
            "--config",
            str(cfg),
            "--state-dir",
            str(state_dir),
            "--no-user-config",
            "--now",
            T0,
            "--json",
        ]
    )
    assert rc == 0
    capsys.readouterr()

    res = router.candidates("claude", include_agents=["claude"], now=T0)
    models = {c["model"] for c in res["candidates"]}
    assert "claude/claude-fable-5-2" not in models
    assert "claude/claude-opus-5-5" in models

    # 2. CLI `agr usage set --agent claude --tier fable-weekly --no-rate-limited` clears tier rejection.
    rc_clear = cli_main(
        [
            "usage",
            "set",
            "--agent",
            "claude",
            "--tier",
            "fable-weekly",
            "--no-rate-limited",
            "--config",
            str(cfg),
            "--state-dir",
            str(state_dir),
            "--no-user-config",
            "--now",
            T_30S,
            "--json",
        ]
    )
    assert rc_clear == 0
    capsys.readouterr()
    res_cleared = router.candidates("claude", include_agents=["claude"], now=T_30S)
    assert "claude/claude-fable-5-2" in {c["model"] for c in res_cleared["candidates"]}

    # 3. Without --tier, plain-text 429 blocks the whole claude weekly window.
    rc_global = cli_main(
        [
            "usage",
            "ingest",
            "--agent",
            "claude",
            "--text",
            f"HTTP 429: You've hit your weekly limit · resets {RESET_7D}",
            "--config",
            str(cfg),
            "--state-dir",
            str(state_dir),
            "--no-user-config",
            "--now",
            T_30S,
            "--json",
        ]
    )
    assert rc_global == 0
    capsys.readouterr()
    with pytest.raises(AgRouteError, match="weekly limit rejected"):
        router.candidates("claude", include_agents=["claude"], now=T_30S)

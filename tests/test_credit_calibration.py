"""Empirical Bayes 5h and weekly credit-pool ceiling calibration (#41)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time
import tomllib

import pytest

from agroute import AgRouteError, Router
from agroute.cli import main
from agroute.discovery import parse_agy_models_output

HOWHARD_CELL_KEYS = {"model", "effort", "usd_per_mtok", "rate_card", "credit_budget", "cost_per_credit"}
HOWHARD_BUDGET_KEYS = {"available_credit_usd", "session_5h_remaining_credit_usd", "weekly_7d_remaining_credit_usd"}


def _seed_models() -> None:
    (Path(os.environ["AGROUTE_SPOOL_DIR"]) / "agy_models_cache.json").write_text(
        json.dumps(
            {
                "format": "agroute-agy-models-cache-v1",
                "fetched_at": time.time(),
                "candidates": parse_agy_models_output(
                    "Fetching available models...\n"
                    "gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)\n"
                    "claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)\n"
                ),
            }
        ),
        encoding="utf-8",
    )
    claude_cat = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "cache/model-catalog/catalog.json"
    claude_cat.parent.mkdir(parents=True, exist_ok=True)
    claude_cat.write_text(
        json.dumps(
            {
                "catalog": {
                    "config": {
                        "models": [
                            {
                                "id": "claude-sonnet-5-5",
                                "section": "main",
                                "runtime": {"effort_levels": ["high"]},
                            },
                            {
                                "id": "claude-fable-5-2",
                                "section": "main",
                                "runtime": {"family": "fable", "effort_levels": ["high"]},
                            },
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (Path(os.environ["CODEX_HOME"]) / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "gpt-6.1-sol",
                        "visibility": "list",
                        "priority": 1,
                        "supported_reasoning_levels": [{"effort": "high"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _ingest_agy_tiers(
    router: Router,
    *,
    now: str,
    g5_pct: float,
    g5_reset: str,
    gw_pct: float,
    gw_reset: str,
    p5_pct: float = 10.0,
    p5_reset: str = "2026-11-12T17:00:00Z",
    pw_pct: float = 10.0,
    pw_reset: str = "2026-11-19T12:00:00Z",
) -> None:
    payload = {
        "plan_tier": "pro-20",
        "quota": {
            "gemini-5h": {"used_pct": g5_pct, "reset_at": g5_reset},
            "gemini-weekly": {"used_pct": gw_pct, "reset_at": gw_reset},
            "3p-5h": {"used_pct": p5_pct, "reset_at": p5_reset},
            "3p-weekly": {"used_pct": pw_pct, "reset_at": pw_reset},
        },
    }
    router.usage_ingest("agy", json.dumps(payload), now=now)


def test_empirical_bayes_shrinkage_updates_5h_and_weekly_pools_and_surfaces_metadata(tmp_path, capsys):
    _seed_models()
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = repo / ".git" / "agroute"
    hh_dir = repo / ".git" / "howhard"
    hh_dir.mkdir(parents=True)

    cfg = repo / "agroute.toml"
    cfg.write_text(
        '[agents.agy]\nplan = "pro-20"\nhost_reserve_pct = 0.0\n',
        encoding="utf-8",
    )
    router = Router(cfg, project=repo, state_directory=state_dir, include_user_config=False)

    # Step 1: Anchor initial snapshot at t0 (`pro-20` gemini priors: 5h = $32.0, weekly = $135.0)
    t0 = "2026-11-12T12:00:00Z"
    _ingest_agy_tiers(
        router,
        now=t0,
        g5_pct=10.0,
        g5_reset="2026-11-12T17:00:00Z",
        gw_pct=20.0,
        gw_reset="2026-11-19T12:00:00Z",
    )
    u0 = router.usage_show(agent="agy", now=t0)["agents"]["agy"]
    m0 = u0["models"]["gemini-4-hydrogen-high"]
    assert m0["credit_calibration"]["session_5h"]["prior_credit_usd"] == pytest.approx(32.0)
    assert m0["credit_calibration"]["session_5h"]["calibrated_credit_usd"] == pytest.approx(32.0)
    assert m0["credit_calibration"]["session_5h"]["effective_sample_windows"] == pytest.approx(0.0)
    assert m0["credit_budget"]["session_5h_remaining_credit_usd"] == pytest.approx(0.90 * 32.0)

    # Step 2: Record completed howhard execution (3.00 USD quota credit) at 12:10, then probe at 12:15
    # with +5.0% 5h usage (`delta_frac = 0.05`) and +1.0% weekly usage (`delta_frac = 0.01`),
    # plus 120s (<600s) jitter on `reset_at`.
    _append_jsonl(
        hh_dir / "outcomes.jsonl",
        {
            "id": "exec-agy-1",
            "decision_id": "dec-agy-1",
            "model": "agy/gemini-4-hydrogen-high",
            "effort": "high",
            "outcome": "clean",
            "observed_at": "2026-11-12T12:10:00Z",
            "needed_credit_usd": 3.00,
        },
    )
    t1 = "2026-11-12T12:15:00Z"
    _ingest_agy_tiers(
        router,
        now=t1,
        g5_pct=15.0,
        g5_reset="2026-11-12T17:02:00Z",  # +120s jitter (<600s) -> same window
        gw_pct=21.0,
        gw_reset="2026-11-19T12:01:30Z",  # +90s jitter (<600s) -> same window
    )

    # Expected empirical Bayes shrinkage (`F0 = 0.20`):
    # 5h: (0.20 * 32.0 + 3.00) / (0.20 + 0.05) = 9.40 / 0.25 = 37.6
    # weekly: (0.20 * 135.0 + 3.00) / (0.20 + 0.01) = 30.0 / 0.21 = 142.857143
    expected_5h_total = 37.6
    expected_7d_total = round(30.0 / 0.21, 6)
    expected_5h_rem = round(0.85 * expected_5h_total, 6)
    expected_7d_rem = round(0.79 * expected_7d_total, 6)

    cal_disk = json.loads((state_dir / "credit_calibration.json").read_text(encoding="utf-8"))
    assert cal_disk["format"] == "agroute-credit-calibration-v2"
    assert cal_disk["pools"]["agy:pro-20:gemini:session_5h"]["matched_intervals"] == 1
    assert cal_disk["pools"]["agy:pro-20:gemini:weekly_7d"]["matched_intervals"] == 1

    # Verify `usage show` surfaces both pool summary and per-model calibrated totals
    u1 = router.usage_show(agent="agy", now=t1)["agents"]["agy"]
    assert u1["credit_calibration"]["gemini"]["session_5h"]["calibrated_credit_usd"] == pytest.approx(
        expected_5h_total
    )
    assert u1["credit_calibration"]["gemini"]["weekly_7d"]["calibrated_credit_usd"] == pytest.approx(
        expected_7d_total
    )
    m1 = u1["models"]["gemini-4-hydrogen-high"]
    assert m1["credit_budget"] == {
        "available_credit_usd": min(expected_5h_rem, expected_7d_rem),
        "session_5h_remaining_credit_usd": expected_5h_rem,
        "weekly_7d_remaining_credit_usd": expected_7d_rem,
    }

    # Verify `candidates --json` and `inventory` (JSON + TOML) keep strict howhard cell keys
    # while exposing `credit_calibration` at the envelope / comment level
    cands_res = router.candidates("agy", include_agents=["agy"], now=t1)
    assert cands_res["credit_calibration"]["agy"]["gemini"]["session_5h"]["matched_intervals"] == 1
    for cell in cands_res["candidates"]:
        assert set(cell) <= HOWHARD_CELL_KEYS
        assert set(cell["credit_budget"]) <= HOWHARD_BUDGET_KEYS
    hyd_cell = next(c for c in cands_res["candidates"] if c["model"] == "agy/gemini-4-hydrogen-high")
    assert hyd_cell["credit_budget"]["session_5h_remaining_credit_usd"] == pytest.approx(expected_5h_rem)
    assert hyd_cell["credit_budget"]["weekly_7d_remaining_credit_usd"] == pytest.approx(expected_7d_rem)

    assert (
        main(
            [
                "inventory",
                "--host",
                "agy",
                "--include-agents",
                "agy",
                "--config",
                str(cfg),
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                t1,
            ]
        )
        == 0
    )
    inv_toml_raw = capsys.readouterr().out
    assert "# credit_calibration agy.gemini:" in inv_toml_raw
    assert "monthly_cost_usd=" in inv_toml_raw
    assert "weekly_cost_usd=" in inv_toml_raw
    assert "cost_per_credit=" in inv_toml_raw
    assert "available_credit_usd=" in inv_toml_raw
    parsed_toml = tomllib.loads(inv_toml_raw)
    assert parsed_toml["inventories"]["router"] == cands_res["candidates"]


def test_sub_threshold_accumulation_and_window_rollover(tmp_path):
    _seed_models()
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = repo / ".git" / "agroute"
    hh_dir = repo / ".git" / "howhard"
    hh_dir.mkdir(parents=True)

    cfg = repo / "agroute.toml"
    cfg.write_text('[agents.codex]\nplan = "plus-20"\nhost_reserve_pct = 0.0\n', encoding="utf-8")
    router = Router(cfg, project=repo, state_directory=state_dir, include_user_config=False)

    # `plus-20` codex priors: session_5h_credit_usd = 20.0, weekly_7d_credit_usd = 50.0
    router.usage_set(
        "codex",
        session_used_pct=10.0,
        session_reset_at="2026-11-12T17:00:00Z",
        now="2026-11-12T12:00:00Z",
    )

    # +0.15% (< 0.25%) does not match yet and keeps `last_snapshot` anchored at 12:00 (`10.0%`)
    _append_jsonl(
        hh_dir / "outcomes.jsonl",
        {
            "id": "codex-small-1",
            "model": "codex/gpt-6.1-sol",
            "effort": "high",
            "outcome": "clean",
            "observed_at": "2026-11-12T12:04:00Z",
            "cost_usd": 0.05,
        },
    )
    router.usage_set(
        "codex",
        session_used_pct=10.15,
        session_reset_at="2026-11-12T17:00:00Z",
        now="2026-11-12T12:05:00Z",
    )
    u_mid = router.usage_show(agent="codex", now="2026-11-12T12:05:00Z")["agents"]["codex"]
    assert u_mid["credit_calibration"]["shared"]["session_5h"]["matched_intervals"] == 0

    # Another task ($0.07) pushes usage to 10.40% (`delta_used_pct = 0.40 >= 0.25` from 12:00 anchor):
    # total interval cost = 0.05 + 0.07 = 0.12 over `delta_frac = 0.004` -> implied $30.0!
    _append_jsonl(
        hh_dir / "outcomes.jsonl",
        {
            "id": "codex-small-2",
            "model": "codex/gpt-6.1-sol",
            "effort": "high",
            "outcome": "clean",
            "observed_at": "2026-11-12T12:08:00Z",
            "cost_usd": 0.07,
        },
    )
    router.usage_set(
        "codex",
        session_used_pct=10.40,
        session_reset_at="2026-11-12T17:00:00Z",
        now="2026-11-12T12:10:00Z",
    )
    u_acc = router.usage_show(agent="codex", now="2026-11-12T12:10:00Z")["agents"]["codex"]
    s5_cal = u_acc["credit_calibration"]["shared"]["session_5h"]
    assert s5_cal["matched_intervals"] == 1
    assert s5_cal["calibrated_credit_usd"] == pytest.approx((0.20 * 20.0 + 0.12) / (0.20 + 0.004))

    # Window rollover when `reset_at` advances by >=600s (or `used_pct` drops): must NOT match across reset
    _append_jsonl(
        hh_dir / "outcomes.jsonl",
        {
            "id": "codex-across-reset",
            "model": "codex/gpt-6.1-sol",
            "effort": "high",
            "outcome": "clean",
            "observed_at": "2026-11-12T17:01:00Z",
            "cost_usd": 1.50,
        },
    )
    router.usage_set(
        "codex",
        session_used_pct=20.0,  # Higher number, but `reset_at` shifted by +5h (>=600s)!
        session_reset_at="2026-11-12T22:00:00Z",
        now="2026-11-12T17:05:00Z",
    )
    u_roll = router.usage_show(agent="codex", now="2026-11-12T17:05:00Z")["agents"]["codex"]
    assert u_roll["credit_calibration"]["shared"]["session_5h"]["matched_intervals"] == 1


def test_amends_measurement_corrections_and_execution_usage_fallback(tmp_path):
    _seed_models()
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = repo / ".git" / "agroute"
    hh_dir = repo / ".git" / "howhard"
    hh_dir.mkdir(parents=True)

    cfg = repo / "agroute.toml"
    cfg.write_text('[agents.claude]\nplan = "pro-20"\nhost_reserve_pct = 0.0\n', encoding="utf-8")
    router = Router(cfg, project=repo, state_directory=state_dir, include_user_config=False)

    # `pro-20` claude session_5h_credit_usd prior = 18.0
    router.usage_set(
        "claude",
        session_used_pct=5.0,
        session_reset_at="2026-11-12T17:00:00Z",
        now="2026-11-12T12:00:00Z",
    )

    # Observation 1: initial pending entry at 12:10 without `cost_usd`, amended at 12:25 with
    # `measurement_corrections.cost_usd = 1.20` (original `observed_at` stays 12:10 inside `(12:00, 12:20]`).
    # Observation 2: `execution_usage` segments carrying `estimated_cost.usd = 0.80` at 12:15.
    _append_jsonl(
        hh_dir / "pending.jsonl",
        {
            "id": "claude-obs-1",
            "model": "claude/claude-sonnet-5-5",
            "effort": "high",
            "outcome": "clean",
            "observed_at": "2026-11-12T12:10:00Z",
        },
    )
    _append_jsonl(
        hh_dir / "pending.jsonl",
        {
            "id": "claude-obs-1-attach",
            "amends": "claude-obs-1",
            "observed_at": "2026-11-12T12:25:00Z",
            "measurement_corrections": {"cost_usd": 1.20, "tokens": 50000},
        },
    )
    _append_jsonl(
        hh_dir / "pending.jsonl",
        {
            "id": "claude-obs-2",
            "model": "claude/claude-sonnet-5-5",
            "effort": "high",
            "outcome": "clean",
            "observed_at": "2026-11-12T12:15:00Z",
            "execution_usage": {
                "provenance": {"completed": True},
                "segments": [{"estimated_cost": {"usd": 0.80}}],
            },
        },
    )

    # Total cost = 2.00 over +10.0% (`delta_frac = 0.10`) -> implied $20.0
    router.usage_set(
        "claude",
        session_used_pct=15.0,
        session_reset_at="2026-11-12T17:00:00Z",
        now="2026-11-12T12:20:00Z",
    )
    s5 = router.usage_show(agent="claude", now="2026-11-12T12:20:00Z")["agents"]["claude"][
        "credit_calibration"
    ]["shared"]["session_5h"]
    assert s5["matched_intervals"] == 1
    assert s5["calibrated_credit_usd"] == pytest.approx((0.20 * 18.0 + 2.00) / (0.20 + 0.10))


def test_straddling_routes_unpriced_observations_and_outliers_are_skipped(tmp_path):
    _seed_models()
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = repo / ".git" / "agroute"
    hh_dir = repo / ".git" / "howhard"
    hh_dir.mkdir(parents=True)

    cfg = repo / "agroute.toml"
    cfg.write_text('[agents.codex]\nplan = "plus-20"\nhost_reserve_pct = 0.0\n', encoding="utf-8")
    router = Router(cfg, project=repo, state_directory=state_dir, include_user_config=False)

    router.usage_set(
        "codex",
        session_used_pct=5.0,
        session_reset_at="2026-11-12T17:00:00Z",
        now="2026-11-12T12:00:00Z",
    )

    # 1. Straddling unobserved route planned at 12:05 -> interval `(12:00, 12:10]` skipped
    router.plan(
        "codex",
        {
            "decision_id": "dec-inflight",
            "chosen": {"model": "codex/gpt-6.1-sol", "effort": "high"},
            "cap_met": True,
            "needs_review": False,
        },
        prompt="inflight work",
        now="2026-11-12T12:05:00Z",
    )
    _append_jsonl(
        hh_dir / "outcomes.jsonl",
        {
            "id": "obs-other",
            "decision_id": "dec-other",
            "model": "codex/gpt-6.1-sol",
            "effort": "high",
            "outcome": "clean",
            "observed_at": "2026-11-12T12:06:00Z",
            "cost_usd": 1.00,
        },
    )
    router.usage_set(
        "codex",
        session_used_pct=10.0,
        session_reset_at="2026-11-12T17:00:00Z",
        now="2026-11-12T12:10:00Z",
    )
    assert (
        router.usage_show(agent="codex", now="2026-11-12T12:10:00Z")["agents"]["codex"][
            "credit_calibration"
        ]["shared"]["session_5h"]["matched_intervals"]
        == 0
    )

    # Complete the straddling route at 12:12 so later intervals are unblocked, then test:
    # 2. Outlier interval (`$50.0` over `1.0%` -> implied `$5000` > `10 * $20 = $200`) rejected!
    _append_jsonl(
        hh_dir / "outcomes.jsonl",
        {
            "id": "obs-inflight-done",
            "decision_id": "dec-inflight",
            "model": "codex/gpt-6.1-sol",
            "effort": "high",
            "outcome": "clean",
            "observed_at": "2026-11-12T12:12:00Z",
            "cost_usd": 50.0,
        },
    )
    router.usage_set(
        "codex",
        session_used_pct=11.0,
        session_reset_at="2026-11-12T17:00:00Z",
        now="2026-11-12T12:15:00Z",
    )
    cal_disk = json.loads((state_dir / "credit_calibration.json").read_text(encoding="utf-8"))
    pool = cal_disk["pools"]["codex:plus-20:shared:session_5h"]
    assert pool["matched_intervals"] == 0
    assert pool["rejected_outliers"] == 1

    # 3. Unpriced observation (`cost_usd = None`) in `(12:15, 12:25]` -> skipped cleanly
    _append_jsonl(
        hh_dir / "outcomes.jsonl",
        {
            "id": "obs-unpriced",
            "decision_id": "dec-unpriced",
            "model": "codex/gpt-6.1-sol",
            "effort": "high",
            "outcome": "clean",
            "observed_at": "2026-11-12T12:20:00Z",
            "cost_usd": None,
        },
    )
    router.usage_set(
        "codex",
        session_used_pct=16.0,
        session_reset_at="2026-11-12T17:00:00Z",
        now="2026-11-12T12:25:00Z",
    )
    assert (
        router.usage_show(agent="codex", now="2026-11-12T12:25:00Z")["agents"]["codex"][
            "credit_calibration"
        ]["shared"]["session_5h"]["matched_intervals"]
        == 0
    )


def test_exponential_forgetting_cap_manual_override_and_clear_preservation(tmp_path):
    _seed_models()
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = repo / ".git" / "agroute"
    hh_dir = repo / ".git" / "howhard"
    hh_dir.mkdir(parents=True)

    # Explicit manual `session_5h_credit_usd = 42.0` override on `plus-20`, leaving `weekly_7d_credit_usd` ($50 prior) calibrated!
    cfg = repo / "agroute.toml"
    cfg.write_text(
        '[agents.codex]\nplan = "plus-20"\nhost_reserve_pct = 0.0\n\n'
        "[agents.codex.credit]\nsession_5h_credit_usd = 42.0\n",
        encoding="utf-8",
    )
    router = Router(cfg, project=repo, state_directory=state_dir, include_user_config=False)

    # Feed 6 weekly intervals of +50% each (`3.0` windows > `F_max = 2.0`), with true ceiling $100 ($50 per 50%):
    base_dt = datetime(2026, 11, 12, tzinfo=timezone.utc)
    for window_idx in range(3):
        day_dt = base_dt + timedelta(days=7 * window_idx)
        w_reset = (day_dt + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        s_reset = (day_dt + timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        t_start = day_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        router.usage_set(
            "codex",
            session_used_pct=10.0,
            session_reset_at=s_reset,
            weekly_used_pct=0.0,
            weekly_reset_at=w_reset,
            now=t_start,
        )
        for step in (1, 2):
            obs_time = (day_dt + timedelta(hours=step)).strftime("%Y-%m-%dT%H:%M:%SZ")
            probe_time = (day_dt + timedelta(hours=step, minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
            _append_jsonl(
                hh_dir / "outcomes.jsonl",
                {
                    "id": f"codex-w-{window_idx}-{step}",
                    "model": "codex/gpt-6.1-sol",
                    "effort": "high",
                    "outcome": "clean",
                    "observed_at": obs_time,
                    "cost_usd": 50.0,
                },
            )
            router.usage_set(
                "codex",
                session_used_pct=10.0 + 10.0 * step,
                session_reset_at=s_reset,
                weekly_used_pct=50.0 * step,
                weekly_reset_at=w_reset,
                now=probe_time,
            )

    u = router.usage_show(agent="codex", now="2026-11-26T03:00:00Z")["agents"]["codex"]
    m_cal = u["models"]["gpt-6.1-sol"]["credit_calibration"]

    # Manual `session_5h_credit_usd = 42.0` stays exact and uncalibrated
    assert m_cal["session_5h"]["manual_override"] is True
    assert m_cal["session_5h"]["calibrated_credit_usd"] == pytest.approx(42.0)

    # `weekly_7d` capped at `effective_sample_windows == 2.0` (`F_max = 2.0`) and converged near $100:
    # `(0.20 * 50 + 200) / (0.20 + 2.0) = 210 / 2.2 = 95.454545`
    assert m_cal["weekly_7d"]["manual_override"] is False
    assert m_cal["weekly_7d"]["effective_sample_windows"] == pytest.approx(2.0)
    assert m_cal["weekly_7d"]["matched_intervals"] == 6
    assert m_cal["weekly_7d"]["calibrated_credit_usd"] == pytest.approx(210.0 / 2.2)

    # `usage clear` resets `last_snapshot` while preserving learned `(s_credit, s_frac)`
    router.usage_clear(agent="codex")
    cal_disk = json.loads((state_dir / "credit_calibration.json").read_text(encoding="utf-8"))
    w_pool = cal_disk["pools"]["codex:plus-20:shared:weekly_7d"]
    assert w_pool["last_snapshot"] is None
    assert w_pool["s_frac"] == pytest.approx(2.0)

    # Unclaimed `credit_calibration.json` in a foreign state directory is refused
    foreign = tmp_path / "foreign-state"
    foreign.mkdir()
    (foreign / "credit_calibration.json").write_text("{}", encoding="utf-8")
    with pytest.raises(AgRouteError, match="unclaimed"):
        Router(cfg, project=repo, state_directory=foreign, include_user_config=False)

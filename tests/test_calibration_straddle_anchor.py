"""Route straddles and in-flight anchor holds during quota-credit calibration (#130)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agroute import Router

# `plus-20` codex priors: session_5h_credit_usd = $20.0, weekly_7d_credit_usd = $50.0.
SESSION_POOL = "codex:plus-20:shared:session_5h"
WEEKLY_POOL = "codex:plus-20:shared:weekly_7d"
SESSION_RESET = "2026-11-12T17:00:00Z"
WEEKLY_RESET = "2026-11-19T12:00:00Z"


@pytest.fixture(autouse=True)
def _no_ambient_howhard_store(monkeypatch):
    monkeypatch.delenv("HOWHARD_STATE_DIR", raising=False)


@pytest.fixture(autouse=True)
def _codex_models():
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


def _seed_repo(tmp_path: Path) -> tuple[Router, Path, Path]:
    repo = tmp_path / "repo"
    hh_dir = repo / ".git" / "howhard"
    hh_dir.mkdir(parents=True)
    state_dir = repo / ".git" / "agroute"
    cfg = repo / "agroute.toml"
    cfg.write_text('[agents.codex]\nplan = "plus-20"\nhost_reserve_pct = 0.0\n', encoding="utf-8")
    router = Router(cfg, project=repo, state_directory=state_dir, include_user_config=False)
    return router, state_dir, hh_dir


def _observe(hh_dir: Path, obs_id: str, decision_id: str | None, observed_at: str, credit_usd: float) -> None:
    record = {
        "id": obs_id,
        "model": "codex/gpt-6.1-sol",
        "effort": "high",
        "outcome": "clean",
        "observed_at": observed_at,
        "needed_credit_usd": credit_usd,
    }
    if decision_id is not None:
        record["decision_id"] = decision_id
    with (hh_dir / "outcomes.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _plan(router: Router, decision_id: str, now: str) -> dict:
    return router.plan(
        "codex",
        {
            "decision_id": decision_id,
            "chosen": {"model": "codex/gpt-6.1-sol", "effort": "high"},
            "cap_met": True,
            "needs_review": False,
        },
        prompt=f"work for {decision_id}",
        now=now,
    )


def _probe_session(router: Router, used_pct: float, now: str) -> None:
    router.usage_set("codex", session_used_pct=used_pct, session_reset_at=SESSION_RESET, now=now)


def _probe_weekly(router: Router, used_pct: float, now: str) -> None:
    router.usage_set("codex", weekly_used_pct=used_pct, weekly_reset_at=WEEKLY_RESET, now=now)


def _pool(state_dir: Path, key: str) -> dict:
    return json.loads((state_dir / "credit_calibration.json").read_text(encoding="utf-8"))["pools"][key]


def test_mid_run_probes_hold_the_anchor_until_the_worker_completes(tmp_path):
    router, state_dir, hh_dir = _seed_repo(tmp_path)
    _probe_session(router, 10.0, "2026-11-12T12:00:00Z")
    _plan(router, "dec-long-run", "2026-11-12T12:01:00Z")

    # Periodic probes while the worker is still running never bisect the execution.
    for now, used in (("2026-11-12T12:04:00Z", 13.0), ("2026-11-12T12:08:00Z", 14.5)):
        _probe_session(router, used, now)
        pool = _pool(state_dir, SESSION_POOL)
        assert pool["matched_intervals"] == 0
        assert pool["last_snapshot"]["observed_at"] == "2026-11-12T12:00:00Z"
        assert pool["last_snapshot"]["used_pct"] == pytest.approx(10.0)

    _observe(hh_dir, "exec-long-run", "dec-long-run", "2026-11-12T12:10:00Z", 2.50)
    _probe_session(router, 15.0, "2026-11-12T12:12:00Z")

    # The full `10.0% -> 15.0%` span is attributed: implied ceiling = $2.50 / 0.05 = $50.0.
    pool = _pool(state_dir, SESSION_POOL)
    assert pool["matched_intervals"] == 1
    assert pool["rejected_outliers"] == 0
    assert pool["s_credit"] == pytest.approx(2.50)
    assert pool["s_frac"] == pytest.approx(0.05)
    assert pool["last_snapshot"]["observed_at"] == "2026-11-12T12:12:00Z"


def test_observed_route_straddling_the_left_boundary_is_rejected_and_the_anchor_advances(tmp_path):
    router, state_dir, hh_dir = _seed_repo(tmp_path)
    # The worker starts before the anchor snapshot, so part of its quota predates `t_prev`.
    _plan(router, "dec-left", "2026-11-12T11:55:00Z")
    _probe_session(router, 14.0, "2026-11-12T12:00:00Z")
    _observe(hh_dir, "exec-left", "dec-left", "2026-11-12T12:05:00Z", 2.00)
    _probe_session(router, 15.0, "2026-11-12T12:10:00Z")

    # Never `$2.00 / 0.01 = $200` from only the post-anchor tail.
    pool = _pool(state_dir, SESSION_POOL)
    assert pool["matched_intervals"] == 0
    assert pool["rejected_outliers"] == 0
    assert pool["s_frac"] == pytest.approx(0.0)
    assert pool["last_snapshot"]["observed_at"] == "2026-11-12T12:10:00Z"
    assert pool["last_snapshot"]["used_pct"] == pytest.approx(15.0)

    # A clean run inside `(12:10, 12:20]` calibrates normally.
    _plan(router, "dec-clean", "2026-11-12T12:12:00Z")
    _observe(hh_dir, "exec-clean", "dec-clean", "2026-11-12T12:15:00Z", 1.00)
    _probe_session(router, 20.0, "2026-11-12T12:20:00Z")
    pool = _pool(state_dir, SESSION_POOL)
    assert pool["matched_intervals"] == 1
    assert pool["s_credit"] == pytest.approx(1.00)
    assert pool["s_frac"] == pytest.approx(0.05)


def test_priced_closed_route_straddling_the_left_boundary_is_rejected(tmp_path):
    router, state_dir, _ = _seed_repo(tmp_path)
    plan = _plan(router, "dec-closed-left", "2026-11-12T11:55:00Z")
    _probe_session(router, 14.0, "2026-11-12T12:00:00Z")
    router.close_route(plan["route_id"], status="done", credit_usd=2.00, now="2026-11-12T12:05:00Z")
    _probe_session(router, 15.0, "2026-11-12T12:10:00Z")

    pool = _pool(state_dir, SESSION_POOL)
    assert pool["matched_intervals"] == 0
    assert pool["rejected_outliers"] == 0
    assert pool["s_frac"] == pytest.approx(0.0)
    assert pool["last_snapshot"]["observed_at"] == "2026-11-12T12:10:00Z"

    # A priced close fully inside the next interval still calibrates.
    clean = _plan(router, "dec-closed-clean", "2026-11-12T12:12:00Z")
    router.close_route(clean["route_id"], status="done", credit_usd=1.00, now="2026-11-12T12:15:00Z")
    _probe_session(router, 20.0, "2026-11-12T12:20:00Z")
    pool = _pool(state_dir, SESSION_POOL)
    assert pool["matched_intervals"] == 1
    assert pool["s_credit"] == pytest.approx(1.00)


def test_expired_unobserved_route_skips_its_interval_without_deadlocking_the_anchor(tmp_path):
    router, state_dir, hh_dir = _seed_repo(tmp_path)
    _probe_weekly(router, 10.0, "2026-11-12T12:00:00Z")
    _plan(router, "dec-abandoned", "2026-11-12T12:05:00Z")

    # Within the 5-hour TTL the route counts as in flight and holds the anchor.
    _probe_weekly(router, 12.0, "2026-11-12T12:30:00Z")
    pool = _pool(state_dir, WEEKLY_POOL)
    assert pool["matched_intervals"] == 0
    assert pool["last_snapshot"]["observed_at"] == "2026-11-12T12:00:00Z"

    # The route never closes. Past its TTL the overlapped interval is contaminated: it is
    # skipped even alongside another priced observation, and the anchor advances.
    _observe(hh_dir, "exec-other", None, "2026-11-12T17:00:00Z", 1.00)
    _probe_weekly(router, 13.0, "2026-11-12T17:10:00Z")
    pool = _pool(state_dir, WEEKLY_POOL)
    assert pool["matched_intervals"] == 0
    assert pool["rejected_outliers"] == 0
    assert pool["last_snapshot"]["observed_at"] == "2026-11-12T17:10:00Z"

    # The expired route no longer blocks later intervals.
    _plan(router, "dec-after", "2026-11-12T17:15:00Z")
    _observe(hh_dir, "exec-after", "dec-after", "2026-11-12T17:20:00Z", 0.50)
    _probe_weekly(router, 14.0, "2026-11-12T17:25:00Z")
    pool = _pool(state_dir, WEEKLY_POOL)
    assert pool["matched_intervals"] == 1
    assert pool["s_credit"] == pytest.approx(0.50)
    assert pool["s_frac"] == pytest.approx(0.01)

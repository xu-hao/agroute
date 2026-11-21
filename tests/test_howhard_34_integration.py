"""Behavioral tests for howhard 3.4 interrupted outcomes, v8/v9 receipts, and flushed outcomes.jsonl (#74)."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from agroute import Router
from agroute.router import _decision_has_credit_cost_split

textual = pytest.importorskip("textual", reason="the TUI dependency is not installed")

from agroute.tui import build_app  # noqa: E402
from textual.widgets import DataTable, Input, OptionList, Static  # noqa: E402


def test_routes_correlate_flushed_outcomes_and_interrupted_cause(tmp_path):
    from conftest import write_howhard_payload_decision

    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    hh_dir = git_dir / "howhard"
    hh_dir.mkdir(parents=True)
    state_dir = git_dir / "agroute"

    dec_flushed = "1111111111111111111111111111111111111111111111111111111111111111"
    dec_amended = "2222222222222222222222222222222222222222222222222222222222222222"
    (hh_dir / "decision-events.jsonl").write_text(
        json.dumps({"at_utc": "2026-11-20T10:00:00Z", "decision_id": dec_flushed, "task_type": "task"})
        + "\n"
        + json.dumps({"at_utc": "2026-11-20T11:00:00Z", "decision_id": dec_amended, "task_type": "task"})
        + "\n",
        encoding="utf-8",
    )
    for dec_id, title, ts in (
        (dec_flushed, "#74 Flushed clean run", "2026-11-20T10:00:00Z"),
        (dec_amended, "#74 Quota hit amended", "2026-11-20T11:00:00Z"),
    ):
        write_howhard_payload_decision(
            hh_dir,
            dec_id,
            {
                "chosen": {"model": "codex/gpt-6.2-sol", "effort": "high"},
                "task_type": "specified-implementation",
                "risk_class": "standard",
                "context": {"task_text": title},
            },
            created_at=ts,
        )

    # Flushed observations live in outcomes.jsonl; a pending amendment in pending.jsonl
    # corrects dec_amended from loud to interrupted (usage_limit) while preserving measurements.
    (hh_dir / "outcomes.jsonl").write_text(
        json.dumps(
            {
                "id": "exec-flushed",
                "decision_id": dec_flushed,
                "observed_at": "2026-11-20T10:05:00Z",
                "outcome": "clean",
                "cause": None,
                "model": "codex/gpt-6.2-sol",
                "effort": "high",
                "tokens": 8000,
                "seconds": 25.0,
                "cost_usd": 0.04,
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "exec-amended",
                "decision_id": dec_amended,
                "observed_at": "2026-11-20T11:05:00Z",
                "outcome": "loud",
                "cause": "unrecorded",
                "model": "codex/gpt-6.2-sol",
                "effort": "high",
                "tokens": 1200,
                "seconds": 6.0,
                "cost_usd": 0.01,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (hh_dir / "pending.jsonl").write_text(
        json.dumps(
            {
                "id": "exec-amended-fix",
                "amends": "exec-amended",
                "observed_at": "2026-11-20T11:10:00Z",
                "outcome": "interrupted",
                "cause": "usage_limit",
                "model": "codex/gpt-6.2-sol",
                "effort": "high",
                "tokens": None,
                "seconds": None,
                "cost_usd": None,
                "note": "5h usage limit exhausted",
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "exec-raw-limit",
                "observed_at": "2026-11-20T11:15:00Z",
                "outcome": "interrupted",
                "cause": "provider_error",
                "model": "claude/claude-opus-5-5",
                "effort": "high",
                "task_type": "task",
                "task_text": "Raw interrupted run",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    router = Router(project=repo, state_directory=state_dir, include_user_config=False)
    snap = router.snapshot(all_repos=False, now="2026-11-20T12:00:00Z")
    assert snap["routed_count"] == 0
    assert snap["observed_count"] == 3

    flushed = router.show_route(dec_flushed[:10], all_repos=False, now="2026-11-20T12:00:00Z")
    assert flushed["status"] == "clean"
    assert flushed["outcome"] == "clean"
    assert flushed["cause"] is None
    assert flushed["tokens"] == 8000

    amended = router.show_route(dec_amended[:10], all_repos=False, now="2026-11-20T12:00:00Z")
    assert amended["status"] == "interrupted"
    assert amended["outcome"] == "interrupted"
    assert amended["cause"] == "usage_limit"
    # Null direct measurements on the amendment must preserve the original run's measurements.
    assert amended["tokens"] == 1200
    assert amended["seconds"] == pytest.approx(6.0)
    assert amended["cost_usd"] == pytest.approx(0.01)

    listed = router.list_routes(all_repos=False, state="interrupted", now="2026-11-20T12:00:00Z")
    assert len(listed["routes"]) == 2
    assert {r["cause"] for r in listed["routes"]} == {"usage_limit", "provider_error"}


def test_calibration_does_not_treat_interrupted_route_as_straddled(tmp_path):
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    hh_dir = git_dir / "howhard"
    hh_dir.mkdir(parents=True)
    state_dir = git_dir / "agroute"
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        """
[agents.codex]
plan = "plus-20"

[[agents.codex.cli_candidates]]
model = "gpt-6.2-sol"
effort = "high"
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=repo, state_directory=state_dir, include_user_config=False)

    # 1. Seed initial usage snapshot at T0 and plan a route that ends as interrupted before T1.
    router.usage_set("codex", session_used_pct=10.0, weekly_used_pct=10.0, now="2026-11-20T10:00:00Z")
    dec_int = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    plan_int = router.plan(
        "agy",
        {
            "format": "howhard-decision-v9",
            "decision_id": dec_int,
            "chosen": {"model": "codex/gpt-6.2-sol", "effort": "high"},
            "cap_met": True,
            "needs_review": False,
        },
        prompt="quota-limited task",
        now="2026-11-20T10:01:00Z",
    )
    assert "<clean|loud|silent|interrupted>" in plan_int["feedback"]["command_template"]

    # Record the interrupted outcome BEFORE T1 so it is finished, not in-flight.
    (hh_dir / "pending.jsonl").write_text(
        json.dumps(
            {
                "id": "exec-int",
                "decision_id": dec_int,
                "observed_at": "2026-11-20T10:02:00Z",
                "outcome": "interrupted",
                "cause": "usage_limit",
                "model": "codex/gpt-6.2-sol",
                "effort": "high",
                "tokens": None,
                "seconds": 2.0,
                "cost_usd": None,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    # Advance baseline anchor at T1 after the interrupted run finished (+1% moves last_snapshot to T1,
    # while skipping the (T0, T1] interval conservatively because exec-int has unknown cost_usd=None).
    router.usage_set("codex", session_used_pct=11.0, weekly_used_pct=11.0, now="2026-11-20T10:05:00Z")
    mid_usage = router.usage_show("codex", now="2026-11-20T10:05:00Z")
    assert mid_usage["agents"]["codex"]["credit_calibration"]["shared"]["session_5h"]["matched_intervals"] == 0

    # 2. Run a normal completed execution in (T1, T2] with measured credit = $5.00 and +25% 5h delta.
    dec_clean = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    router.plan(
        "agy",
        {
            "format": "howhard-decision-v9",
            "decision_id": dec_clean,
            "chosen": {"model": "codex/gpt-6.2-sol", "effort": "high"},
            "cap_met": True,
            "needs_review": False,
        },
        prompt="completed task",
        now="2026-11-20T10:06:00Z",
    )
    with (hh_dir / "pending.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "id": "exec-clean",
                    "decision_id": dec_clean,
                    "observed_at": "2026-11-20T10:08:00Z",
                    "outcome": "clean",
                    "cause": None,
                    "model": "codex/gpt-6.2-sol",
                    "effort": "high",
                    "tokens": 50000,
                    "seconds": 30.0,
                    "cost_usd": 5.0,
                }
            )
            + "\n"
        )

    router.usage_set("codex", session_used_pct=36.0, weekly_used_pct=21.0, now="2026-11-20T10:10:00Z")
    usage = router.usage_show("codex", now="2026-11-20T10:10:00Z")
    s5_cal = usage["agents"]["codex"]["credit_calibration"]["shared"]["session_5h"]
    # Because exec-int was recognized as observed (not straddled), the clean interval calibrated!
    assert s5_cal["matched_intervals"] == 1


@pytest.mark.parametrize("fmt", ["howhard-decision-v7", "howhard-decision-v8", "howhard-decision-v9"])
def test_plan_recognizes_v7_v8_v9_credit_cost_split_formats(tmp_path, fmt):
    assert _decision_has_credit_cost_split({"format": fmt}) is True
    assert _decision_has_credit_cost_split({"format": "howhard-decision-v6"}) is False

    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        """
[agents.codex]
plan = "plus-20"

[[agents.codex.cli_candidates]]
model = "gpt-6.2-sol"
effort = "high"
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=tmp_path, state_directory=tmp_path / "state", include_user_config=False)
    router.usage_set("codex", session_used_pct=10.0, weekly_used_pct=10.0, now="2026-11-20T12:00:00Z")

    # Legacy C_run must NOT be misread as needed_credit_usd on v7/v8/v9 receipts.
    plan = router.plan(
        "agy",
        {
            "format": fmt,
            "decision_id": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
            "chosen": {"model": "codex/gpt-6.2-sol", "effort": "high"},
            "cells": [
                {
                    "model": "codex/gpt-6.2-sol",
                    "effort": "high",
                    "C_run": 999.0,
                    "session_cost_usd": 0.25,
                    "needed_credit_usd": 2.0,
                }
            ],
            "cap_met": True,
            "needs_review": False,
        },
        prompt="v8/v9 check",
        now="2026-11-20T12:00:00Z",
    )
    assert plan["needed_credit_usd"] == pytest.approx(2.0)
    assert plan["session_cost_usd"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_tui_renders_interrupted_status_cause_filter_and_history_toggle():
    snap = {
        "format": "agroute-snapshot-v1",
        "captured_at": "2026-11-20T12:00:00Z",
        "default_host": "agy",
        "routed_count": 1,
        "observed_count": 1,
        "agents": {
            name: {
                "enabled": True,
                "eligible_as_target": True,
                "session_5h": {"used_pct": 10.0, "reset_at": None},
                "weekly_7d": {"used_pct": 20.0, "reset_at": None},
                "rate_limited": False,
                "cooldown_until": None,
                "tiers": {},
            }
            for name in ("codex", "claude", "agy")
        },
        "routes": [
            {
                "route_id": "route-limit00001",
                "decision_id": "dec-limit-1",
                "status": "interrupted",
                "execution_mode": "cli",
                "repository": "example-project",
                "repository_id": "repo-example",
                "host_agent": "agy",
                "target_agent": "claude",
                "model": "claude-opus-5-5",
                "qualified_model": "claude/claude-opus-5-5",
                "effort": "high",
                "task_type": "specified-implementation",
                "risk_class": "standard",
                "role": "Worker",
                "label": "Hit 5h usage limit",
                "cap_met": True,
                "needs_review": False,
                "host_affinity_applied": False,
                "created_at": "2026-11-20T11:55:00Z",
                "observed_at": "2026-11-20T11:56:00Z",
                "outcome": "interrupted",
                "cause": "usage_limit",
                "execution_id": "exec-limit-1",
                "tokens": None,
                "seconds": 4.0,
                "cost_usd": None,
                "invocation": None,
                "forecasts": [],
                "note": "5h quota window exhausted",
                "task_text": "Hit 5h usage limit",
            },
            {
                "route_id": "route-active0002",
                "decision_id": "dec-active-2",
                "status": "routed",
                "execution_mode": "native",
                "repository": "example-project",
                "repository_id": "repo-example",
                "host_agent": "agy",
                "target_agent": "agy",
                "model": "gemini-4-argon-high",
                "qualified_model": "agy/gemini-4-argon-high",
                "effort": "high",
                "task_type": "specified-implementation",
                "risk_class": "standard",
                "role": "Worker",
                "label": "Active task",
                "cap_met": True,
                "needs_review": False,
                "host_affinity_applied": False,
                "created_at": "2026-11-20T11:50:00Z",
                "observed_at": None,
                "outcome": None,
                "cause": None,
                "execution_id": None,
                "tokens": None,
                "seconds": None,
                "cost_usd": None,
                "invocation": None,
                "forecasts": [],
                "note": None,
                "task_text": "Active task",
            },
        ],
    }

    app = build_app(lambda: deepcopy(snap), refresh_interval=60.0)
    async with app.run_test(size=(120, 28)) as pilot:
        await pilot.pause()

        table = app.query_one("#routes", DataTable)
        assert table.row_count == 2
        detail = str(app.query_one("#route-detail", Static).render())
        assert "outcome: interrupted (usage_limit)" in detail

        # Route detail modal shows both Outcome and Cause.
        await pilot.press("enter")
        await pilot.pause()
        modal = str(app.screen.query_one("#show-body", Static).render())
        assert "Outcome: interrupted" in modal
        assert "Cause: usage_limit" in modal
        await pilot.press("escape")
        await pilot.pause()

        # Keys help includes interrupted description.
        await pilot.press("question_mark")
        await pilot.pause()
        keys_body = str(app.screen.query_one("#show-body", Static).render())
        assert "interrupted" in keys_body
        await pilot.press("enter")
        await pilot.pause()

        # State filter ('s') supports filtering to interrupted.
        await pilot.press("s")
        await pilot.pause()
        assert app.screen.query_one("#filter-query", Input) is not None
        await pilot.press("i", "n", "t", "e", "r", "r", "u", "p", "t", "e", "d")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert table.row_count == 1

        # Reset state filter and confirm 'h' hides interrupted as an observed status.
        await pilot.press("s")
        await pilot.pause()
        options = app.screen.query_one("#filter-options", OptionList)
        options.highlighted = 0
        await pilot.press("enter")
        await pilot.pause()
        assert table.row_count == 2
        await pilot.press("h")
        await pilot.pause()
        assert table.row_count == 1

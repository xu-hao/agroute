"""Behavioral tests for ROUTE (agroute) vs OUTCOME (howhard) separation and `agr close` (#82)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agroute import AgRouteError, Router
from agroute.cli import main

textual = pytest.importorskip("textual", reason="the TUI dependency is not installed")
from agroute.tui import build_app  # noqa: E402
from textual.widgets import DataTable, Input, OptionList, Static  # noqa: E402


def _seed_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    hh_dir = git_dir / "howhard"
    hh_dir.mkdir(parents=True)
    state_dir = git_dir / "agroute"
    cfg = repo / "agroute.toml"
    cfg.write_text(
        '[routing]\nhost_affinity_epsilon = 0.05\n\n'
        '[agents.agy]\nplan = "ultra-200"\n'
        '[[agents.agy.native_candidates]]\nmodel = "gemini-4-carbon-high"\neffort = "high"\n'
        '[[agents.agy.native_candidates]]\nmodel = "gemini-4-argon-high"\neffort = "high"\n'
        '[[agents.agy.native_candidates]]\nmodel = "claude-opus-5-5-medium"\neffort = "medium"\n\n'
        '[agents.codex]\nplan = "plus-20"\n'
        '[[agents.codex.cli_candidates]]\nmodel = "gpt-6.2-sol"\neffort = "high"\n',
        encoding="utf-8",
    )
    return repo, hh_dir, state_dir


def _append_jsonl(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj) + "\n")


def test_agr_close_links_readable_raw_obs_handles_model_switch_and_releases_cancelled_reservation(
    tmp_path, capsys
):
    repo, hh_dir, state_dir = _seed_repo(tmp_path)
    cfg = repo / "agroute.toml"
    router = Router(cfg, project=repo, state_directory=state_dir, include_user_config=False)
    now0 = "2026-11-21T01:00:00Z"
    for agent in ("agy", "codex"):
        router.usage_set(
            agent,
            session_used_pct=10.0,
            session_reset_at="2026-11-21T06:00:00Z",
            weekly_used_pct=20.0,
            weekly_reset_at="2026-11-27T00:00:00Z",
            now=now0,
        )

    # 1. Host-affinity override (`raw_native`): plan emits human-readable <EXECUTION_ID> + close_command_template.
    dec_aff = "a" * 64
    plan_aff = router.plan(
        "agy",
        {
            "format": "howhard-decision-v7",
            "decision_id": dec_aff,
            "chosen": {"model": "codex/gpt-6.2-sol", "effort": "high"},
            "cap_met": True,
            "needs_review": False,
            "candidates": [
                {
                    "model": "codex/gpt-6.2-sol",
                    "effort": "high",
                    "cap_met": True,
                    "J": 1.00,
                    "session_cost_usd": 0.05,
                    "needed_credit_usd": 0.50,
                },
                {
                    "model": "agy/gemini-4-carbon-high",
                    "effort": "high",
                    "cap_met": True,
                    "J": 1.02,
                    "session_cost_usd": 0.04,
                    "needed_credit_usd": 1.20,
                },
            ],
        },
        prompt="Implement feature A",
        now="2026-11-21T01:01:00Z",
    )
    assert plan_aff["host_affinity_applied"] is True
    assert "<EXECUTION_ID>" in plan_aff["feedback"]["command_template"]
    assert plan_aff["feedback"]["close_command_template"] == [
        "agr",
        "close",
        plan_aff["route_id"],
        "--obs",
        "<EXECUTION_ID>",
        "--json",
    ]

    # Record a human-readable `--native` observation (`decision_id: null`) in howhard.
    _append_jsonl(
        hh_dir / "pending.jsonl",
        {
            "id": "feature-a-carbon-run",
            "decision_id": None,
            "observed_at": "2026-11-21T01:05:00+00:00",
            "outcome": "clean",
            "model": "agy/gemini-4-carbon-high",
            "effort": "high",
            "tokens": 24000,
            "seconds": 62.0,
            "cost_usd": 0.09,
            "credit_usd": 1.10,
            "note": "Completed cleanly via host affinity",
        },
    )

    # Before `agr close`, they are 2 unlinked rows (`routed` + standalone `raw`).
    snap_before = router.snapshot(all_repos=False, now="2026-11-21T01:06:00Z")
    assert len(snap_before["routes"]) == 2

    # Link explicitly with `agr close <route_id> --obs feature-a-carbon-run --json`.
    assert (
        main(
            [
                "close",
                plan_aff["route_id"],
                "--config",
                str(cfg),
                "--state-dir",
                str(state_dir),
                "--no-user-config",
                "--now",
                "2026-11-21T01:06:00Z",
                "--json",
                "--obs",
                "feature-a-carbon-run",
            ]
        )
        == 0
    )
    close_out = json.loads(capsys.readouterr().out)
    assert close_out["route_id"] == plan_aff["route_id"]
    assert close_out["close_status"] == "done"
    assert close_out["linked_execution_id"] == "feature-a-carbon-run"

    # Now collapsed into 1 row with `route_status == "done"`, `outcome == "clean"`, `outcome_source == "linked_raw"`.
    snap_after = router.snapshot(all_repos=False, now="2026-11-21T01:06:30Z")
    assert len(snap_after["routes"]) == 1
    r_aff = snap_after["routes"][0]
    assert r_aff["route_id"] == plan_aff["route_id"]
    assert r_aff["route_status"] == "done"
    assert r_aff["outcome"] == "clean"
    assert r_aff["outcome_source"] == "linked_raw"
    assert r_aff["execution_id"] == "feature-a-carbon-run"
    assert r_aff["tokens"] == 24000

    # 2. Mid-task in-place model switch: Leg 1 (`interrupted` in howhard + `switched` in agroute) -> Leg 2 (`done` in agroute, unscored in howhard).
    dec_leg1 = "b" * 64
    plan_leg1 = router.plan(
        "agy",
        {
            "format": "howhard-decision-v7",
            "decision_id": dec_leg1,
            "chosen": {"model": "agy/claude-opus-5-5-medium", "effort": "medium"},
            "cap_met": False,
            "needs_review": True,
            "needed_credit_usd": 2.0,
            "session_cost_usd": 0.07,
        },
        prompt="Ship release 0.5.3",
        now="2026-11-21T01:10:00Z",
    )
    dec_leg2 = "c" * 64
    plan_leg2 = router.plan(
        "agy",
        {
            "format": "howhard-decision-v7",
            "decision_id": dec_leg2,
            "chosen": {"model": "agy/gemini-4-argon-high", "effort": "high"},
            "cap_met": True,
            "needs_review": False,
            "needed_credit_usd": 3.0,
            "session_cost_usd": 0.10,
        },
        prompt="Ship release 0.5.3",
        now="2026-11-21T01:15:00Z",
    )
    _append_jsonl(
        hh_dir / "pending.jsonl",
        {
            "id": "leg1-opus-429",
            "decision_id": dec_leg1,
            "observed_at": "2026-11-21T01:14:30+00:00",
            "outcome": "interrupted",
            "cause": "usage_limit",
            "model": "agy/claude-opus-5-5-medium",
            "effort": "medium",
            "tokens": 15000,
            "seconds": 270.0,
        },
    )
    router.close_route(
        plan_leg1["route_id"],
        status="switched",
        superseded_by=plan_leg2["route_id"],
        note="3p-5h hit 100% mid-task; switched in-place to Argon",
        now="2026-11-21T01:15:05Z",
    )
    router.close_route(
        plan_leg2["route_id"],
        status="done",
        switched_from=plan_leg1["route_id"],
        tokens=32000,
        seconds=410.0,
        credit_usd=2.40,
        note="Completed switched session cleanly without scoring mixed howhard receipt",
        now="2026-11-21T01:22:00Z",
    )

    r1 = router.show_route(plan_leg1["route_id"], all_repos=False, now="2026-11-21T01:23:00Z")
    r2 = router.show_route(plan_leg2["route_id"], all_repos=False, now="2026-11-21T01:23:00Z")
    assert r1["route_status"] == "switched"
    assert r1["outcome"] == "interrupted"
    assert r1["superseded_by"] == plan_leg2["route_id"]
    assert r2["route_status"] == "done"
    assert r2["outcome"] == "unscored"
    assert r2["switched_from"] == plan_leg1["route_id"]
    assert r2["tokens"] == 32000
    assert r2["credit_usd"] == pytest.approx(2.40)

    # 2b. Direct in-place switch WITHOUT a second `agr plan`: `agr close <route-1> --switched-to ...`
    # marks leg 1 `switched` and spawns a live continuation route (`routed` / `None`) that closes to `done` / `unscored`.
    dec_direct = "9" * 64
    plan_direct = router.plan(
        "agy",
        {
            "format": "howhard-decision-v7",
            "decision_id": dec_direct,
            "chosen": {"model": "agy/claude-opus-5-5-medium", "effort": "medium"},
            "cap_met": True,
            "needs_review": False,
        },
        prompt="Direct swarm model switch task",
        now="2026-11-21T01:23:10Z",
    )
    direct_sw = router.close_route(
        plan_direct["route_id"],
        switched_to="agy/gemini-4-argon-high",
        effort="high",
        note="Switched directly via swarm(action='model')",
        now="2026-11-21T01:24:00Z",
    )
    assert direct_sw["close_status"] == "switched"
    cont_id = direct_sw["continuation_route_id"]
    assert cont_id.startswith("route-")

    # While Model 2 is running, leg 1 is `switched` and continuation route is live `routed` (`outcome=None`).
    r_direct_1 = router.show_route(plan_direct["route_id"], all_repos=False, now="2026-11-21T01:24:10Z")
    r_direct_2_live = router.show_route(cont_id, all_repos=False, now="2026-11-21T01:24:10Z")
    assert r_direct_1["route_status"] == "switched"
    assert r_direct_1["superseded_by"] == cont_id
    assert r_direct_2_live["route_status"] == "routed"
    assert r_direct_2_live["outcome"] is None
    assert r_direct_2_live["model"] == "gemini-4-argon-high"
    assert r_direct_2_live["switched_from"] == plan_direct["route_id"]

    # When Model 2 finishes, `agr close <cont_id>` moves it to `done` / `unscored`.
    router.close_route(
        cont_id,
        tokens=19000,
        seconds=180.0,
        credit_usd=1.40,
        now="2026-11-21T01:24:50Z",
    )
    r_direct_2_done = router.show_route(cont_id, all_repos=False, now="2026-11-21T01:24:55Z")
    assert r_direct_2_done["route_status"] == "done"
    assert r_direct_2_done["outcome"] == "unscored"

    # 3. Cancelled plan immediately releases its in-flight credit reservation!
    before_cancel_budget = {
        c["model"]: c["credit_budget"]["available_credit_usd"]
        for c in router.candidates("agy", include_agents=["codex"], now="2026-11-21T01:25:00Z")["candidates"]
    }["codex/gpt-6.2-sol"]

    dec_cancel = "d" * 64
    plan_cancel = router.plan(
        "agy",
        {
            "format": "howhard-decision-v7",
            "decision_id": dec_cancel,
            "chosen": {"model": "codex/gpt-6.2-sol", "effort": "high",
                       "needed_credit_usd": 4.0, "session_cost_usd": 0.37},
            "cap_met": False,
            "needs_review": True,
            "needed_credit_usd": 4.0,
            "session_cost_usd": 0.37,
        },
        prompt="Risky migration declined on review",
        now="2026-11-21T01:25:10Z",
    )
    during_plan_budget = {
        c["model"]: c["credit_budget"]["available_credit_usd"]
        for c in router.candidates("agy", include_agents=["codex"], now="2026-11-21T01:25:20Z")["candidates"]
    }["codex/gpt-6.2-sol"]
    assert during_plan_budget == pytest.approx(before_cancel_budget - 4.0)

    router.close_route(
        plan_cancel["route_id"],
        status="cancelled",
        note="Declined after needs_review inspection",
        now="2026-11-21T01:25:30Z",
    )
    after_cancel_budget = {
        c["model"]: c["credit_budget"]["available_credit_usd"]
        for c in router.candidates("agy", include_agents=["codex"], now="2026-11-21T01:25:40Z")["candidates"]
    }["codex/gpt-6.2-sol"]
    assert after_cancel_budget == pytest.approx(before_cancel_budget)

    r_cancel = router.show_route(plan_cancel["route_id"], all_repos=False, now="2026-11-21T01:26:00Z")
    assert r_cancel["route_status"] == "cancelled"
    assert r_cancel["outcome"] is None

    # Snapshot has 0 active routed rows and 6 completed/closed rows.
    snap_final = router.snapshot(all_repos=False, now="2026-11-21T01:26:00Z")
    assert snap_final["routed_count"] == 0
    assert snap_final["observed_count"] == 6


@pytest.mark.asyncio
async def test_tui_renders_separate_route_and_outcome_columns_and_done_review_marker(tmp_path):
    repo, hh_dir, state_dir = _seed_repo(tmp_path)
    router = Router(repo / "agroute.toml", project=repo, state_directory=state_dir, include_user_config=False)
    now0 = "2026-11-21T02:00:00Z"
    for agent in ("agy", "codex"):
        router.usage_set(
            agent,
            session_used_pct=5.0,
            session_reset_at="2026-11-21T07:00:00Z",
            weekly_used_pct=10.0,
            weekly_reset_at="2026-11-27T00:00:00Z",
            now=now0,
        )

    # Review-flagged route that subsequently completed cleanly -> ROUTE shows `done*`, OUTCOME shows `clean`.
    dec_rev_done = "e" * 64
    plan_rev_done = router.plan(
        "agy",
        {
            "decision_id": dec_rev_done,
            "chosen": {"model": "agy/gemini-4-carbon-high", "effort": "high"},
            "cap_met": False,
            "needs_review": True,
        },
        prompt="Reviewed contract change",
        now="2026-11-21T02:01:00Z",
    )
    _append_jsonl(
        hh_dir / "pending.jsonl",
        {
            "id": "rev-done-exec",
            "decision_id": dec_rev_done,
            "observed_at": "2026-11-21T02:05:00+00:00",
            "outcome": "clean",
            "model": "agy/gemini-4-carbon-high",
            "effort": "high",
        },
    )

    # Active in-flight route -> ROUTE shows `routed`, OUTCOME shows `—`.
    dec_active = "f" * 64
    router.plan(
        "agy",
        {
            "decision_id": dec_active,
            "chosen": {"model": "agy/gemini-4-argon-high", "effort": "high"},
            "cap_met": True,
            "needs_review": False,
        },
        prompt="In-flight task",
        now="2026-11-21T02:06:00Z",
    )

    app = build_app(
        lambda: router.snapshot(all_repos=False, now="2026-11-21T02:07:00Z"),
        refresh_interval=60.0,
    )
    async with app.run_test(size=(120, 28)) as pilot:
        await pilot.pause()
        table = app.query_one("#routes", DataTable)
        col_labels = [str(col.label) for col in table.ordered_columns]
        assert col_labels[:3] == ["ROUTE", "OUTCOME", "MODE"]
        assert table.row_count == 2

        rev_row = table.get_row(plan_rev_done["route_id"])
        assert rev_row[0].plain == "done*"
        assert rev_row[1].plain == "clean"

        # `?` help modal explains both ROUTE (agroute) and OUTCOME (howhard).
        await pilot.press("question_mark")
        await pilot.pause()
        keys_text = str(app.screen.query_one("#show-body", Static).render())
        assert "ROUTE" in keys_text and "OUTCOME" in keys_text
        assert "switched" in keys_text and "cancelled" in keys_text
        await pilot.press("escape")
        await pilot.pause()

        # Filter by `done` in state picker ('s').
        await pilot.press("s")
        await pilot.pause()
        assert app.screen.query_one("#filter-query", Input) is not None
        await pilot.press("d", "o", "n", "e")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert table.row_count == 1

        # Reset filter and press `h` to hide completed history -> only active `routed` remains.
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


def test_native_launch_fallback_includes_raw_feedback_and_route_link(tmp_path):
    repo, _, state_dir = _seed_repo(tmp_path)
    router = Router(repo / "agroute.toml", project=repo, state_directory=state_dir, include_user_config=False)
    plan = router.plan("agy", {
        "decision_id": "fallback-decision",
        "chosen": {"model": "agy/gemini-4-carbon-high", "effort": "high"},
    }, prompt="Fallback task", now="2026-11-21T02:00:00Z")
    feedback = plan["fallback"]["feedback"]
    assert feedback["mode"] == "raw_native"
    assert feedback["decision_id"] is None
    assert "--native" in feedback["command_template"]
    assert feedback["close_command_template"] == [
        "agr", "close", plan["route_id"], "--obs", "<EXECUTION_ID>", "--json",
    ]

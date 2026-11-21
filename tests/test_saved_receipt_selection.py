"""Behavioral tests for reading saved howhard receipt selection blocks (#77)."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from agroute import Router
from agroute.routes import _extract_forecasts

textual = pytest.importorskip("textual", reason="the TUI dependency is not installed")

from agroute.tui import build_app  # noqa: E402
from textual.widgets import Static  # noqa: E402


def _saved_v9_receipt(*, cap_met: bool = False, needs_review: bool = True) -> dict:
    """Shape stored in howhard 4.0 payloads.sqlite3."""
    return {
        "format": "howhard-decision-v9",
        "project": "example",
        "project_identity": "0123456789abcdef0123456789abcdef",
        "task_type": "contract-change",
        "risk_class": "contract",
        "chosen": {"model": "codex/gpt-6.2-sol", "family": "gpt", "effort": "high"},
        "seed": 42,
        "context": {
            "encoder": "howhard-context-v1",
            "task_text": "#77 Verify saved receipt correlation",
            "project_context": {"text": "example", "parameters": {}, "fingerprint": "abc"},
        },
        "cost_policy": {
            "worker_money_included": True,
            "unknown_price_models": [],
            "basis": "estimate:estimate",
            "comparison": "full-cost",
        },
        "native_candidates": [
            {"model": "codex/gpt-6.2-sol", "effort": "high", "cost_per_credit": 0.1},
            {"model": "agy/gemini-4-argon-high", "effort": "high", "cost_per_credit": 0.05},
        ],
        "semantic": {
            "format": "howhard-semantic-v6",
            "worlds": {
                "llm": [
                    {"family": "gpt", "effort": "high", "clean": 0.90, "loud": 0.07, "silent": 0.03, "tokens": 10000, "seconds": 30},
                    {"family": "gemini", "effort": "high", "clean": 0.89, "loud": 0.08, "silent": 0.03, "tokens": 10000, "seconds": 30},
                ]
            },
        },
        "selection": {
            "objective": "cost-to-accepted-result",
            "retry_policy": "same-cell-until-non-loud",
            "cap_met": cap_met,
            "needs_review": needs_review,
            "on_cap_unmet": "flag-review",
            "cells": [
                {
                    "model": "codex/gpt-6.2-sol",
                    "family": "gpt",
                    "effort": "high",
                    "P_clean": {"mean": 0.90, "lower": 0.72, "upper": 0.98},
                    "P_loud": {"mean": 0.07, "lower": 0.01, "upper": 0.22},
                    "P_silent": {"mean": 0.03, "lower": 0.002, "upper": 0.12},
                    "C_run": 0.20,
                    "session_cost_usd": 0.20,
                    "cost_per_credit": 0.10,
                    "needed_credit_usd": 2.0,
                    "credit_budget": {"available_credit_usd": 10.0},
                    "available_credit_usd": 10.0,
                    "credit_ok": True,
                    "T": 0.01,
                    "expected_attempts": 1.075,
                    "J": 40.0,
                    "sampled_J": 40.0,
                    "admissible": cap_met,
                },
                {
                    "model": "agy/gemini-4-argon-high",
                    "family": "gemini",
                    "effort": "high",
                    "P_clean": {"mean": 0.89, "lower": 0.70, "upper": 0.98},
                    "P_loud": {"mean": 0.08, "lower": 0.01, "upper": 0.24},
                    "P_silent": {"mean": 0.03, "lower": 0.002, "upper": 0.13},
                    "C_run": 0.10,
                    "session_cost_usd": 0.10,
                    "cost_per_credit": 0.05,
                    "needed_credit_usd": 2.0,
                    "credit_budget": {"available_credit_usd": 100.0},
                    "available_credit_usd": 100.0,
                    "credit_ok": True,
                    "T": 0.01,
                    "expected_attempts": 1.087,
                    "J": 41.0,
                    "sampled_J": 41.0,
                    "admissible": True,
                },
            ],
        },
    }


def test_routes_extract_selection_cells_cap_met_and_needs_review_from_saved_receipt(tmp_path):
    from conftest import write_howhard_payload_decision

    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    hh_dir = git_dir / "howhard"
    hh_dir.mkdir(parents=True)
    state_dir = git_dir / "agroute"

    dec_id = "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
    receipt = _saved_v9_receipt(cap_met=False, needs_review=True)
    (hh_dir / "decision-events.jsonl").write_text(
        json.dumps({"at_utc": "2026-11-21T00:00:00Z", "decision_id": dec_id, "task_type": "contract-change"}) + "\n",
        encoding="utf-8",
    )
    write_howhard_payload_decision(hh_dir, dec_id, receipt, created_at="2026-11-21T00:00:00Z")

    # _extract_forecasts reads selection.cells directly, and falls back to semantic.worlds.llm.
    extracted = _extract_forecasts(receipt)
    assert len(extracted) == 2
    assert extracted[0]["model"] == "codex/gpt-6.2-sol"
    worlds_only = {k: v for k, v in receipt.items() if k != "selection"}
    assert len(_extract_forecasts(worlds_only)) == 2

    router = Router(project=repo, state_directory=state_dir, include_user_config=False)
    route = router.show_route(dec_id[:10], all_repos=False, now="2026-11-21T00:05:00Z")
    assert route["status"] == "review"
    assert route["cap_met"] is False
    assert route["needs_review"] is True
    assert route["session_cost_usd"] == pytest.approx(0.20)
    assert route["needed_credit_usd"] == pytest.approx(2.0)
    assert route["cost_per_credit"] == pytest.approx(0.10)
    assert route["credit_budget"] == {"available_credit_usd": 10.0}
    assert len(route["forecasts"]) == 2


def test_plan_reads_selection_cells_cap_met_needs_review_and_host_affinity_from_saved_receipt(tmp_path):
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        """
[routing]
host_affinity_epsilon = 5.0

[agents.codex]
plan = "plus-20"

[[agents.codex.cli_candidates]]
model = "gpt-6.2-sol"
effort = "high"

[agents.agy]
plan = "ultra-200"

[[agents.agy.native_candidates]]
model = "gemini-4-argon-high"
effort = "high"
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=tmp_path, state_directory=tmp_path / "state", include_user_config=False)
    router.usage_set("codex", session_used_pct=10.0, weekly_used_pct=10.0, now="2026-11-21T00:00:00Z")
    router.usage_set("agy", session_used_pct=5.0, weekly_used_pct=5.0, now="2026-11-21T00:00:00Z")

    receipt = _saved_v9_receipt(cap_met=False, needs_review=True)
    receipt["decision_id"] = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"

    # Saved receipt has cells/cap_met/needs_review only under `selection`:
    # host_affinity_epsilon=5.0 sees agy J=41.0 within 5.0 of codex J=40.0 and switches to native agy.
    plan = router.plan("agy", receipt, prompt="saved receipt plan", now="2026-11-21T00:00:00Z")
    assert plan["cap_met"] is False
    assert plan["needs_review"] is True
    assert plan["host_affinity_applied"] is True
    assert plan["target_agent"] == "agy"
    assert plan["needed_credit_usd"] == pytest.approx(2.0)
    assert plan["session_cost_usd"] == pytest.approx(0.10)


@pytest.mark.asyncio
async def test_tui_modal_formats_selection_cell_forecast_means(tmp_path):
    from conftest import write_howhard_payload_decision

    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    hh_dir = git_dir / "howhard"
    hh_dir.mkdir(parents=True)
    state_dir = git_dir / "agroute"

    dec_id = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    (hh_dir / "decision-events.jsonl").write_text(
        json.dumps({"at_utc": "2026-11-21T00:00:00Z", "decision_id": dec_id, "task_type": "contract-change"}) + "\n",
        encoding="utf-8",
    )
    write_howhard_payload_decision(
        hh_dir, dec_id, _saved_v9_receipt(cap_met=False, needs_review=True), created_at="2026-11-21T00:00:00Z"
    )

    router = Router(project=repo, state_directory=state_dir, include_user_config=False)
    app = build_app(lambda: deepcopy(router.snapshot(all_repos=False, now="2026-11-21T00:05:00Z")), refresh_interval=60.0)
    async with app.run_test(size=(120, 30)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()
        detail = str(app.query_one("#route-detail", Static).render())
        assert "review" in detail
        assert "session_cost_usd=$0.2000" in detail
        assert "needed_credit_usd=$2.0000 credit" in detail

        await pilot.press("enter")
        await pilot.pause()
        modal = str(app.screen.query_one("#show-body", Static).render())
        assert "Forecast panel (top 8):" in modal
        assert "codex/gpt-6.2-sol (high): clean=0.900 loud=0.070 silent=0.030" in modal
        assert "agy/gemini-4-argon-high (high): clean=0.890 loud=0.080 silent=0.030" in modal
        assert "Cap met: False" in modal
        assert "Needs review: yes" in modal

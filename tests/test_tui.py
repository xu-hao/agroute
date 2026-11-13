"""Real-Textual behavioral tests for the realtime model-route view (agroute tui)."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from agroute import AgRouteError, Router
from agroute.discovery import (
    discover_claude_candidates,
    discover_codex_candidates,
    parse_agy_models_output,
)

textual = pytest.importorskip("textual", reason="the TUI dependency is not installed")

from agroute.tui import build_app  # noqa: E402
from textual.widgets import DataTable, Input, OptionList, Static  # noqa: E402


def _make_snapshot() -> dict:
    return {
        "format": "agroute-snapshot-v1",
        "captured_at": "2026-11-08T21:00:00Z",
        "default_host": "agy",
        "routed_count": 2,
        "observed_count": 1,
        "agents": {
            "codex": {
                "enabled": True,
                "eligible_as_target": True,
                "target_exclusion_reason": None,
                "eligible_as_host_native": True,
                "host_native_exclusion_reason": None,
                "session_5h": {"used_pct": 42.0, "reset_at": "2026-11-09T01:00:00Z"},
                "weekly_7d": {"used_pct": 18.0, "reset_at": "2026-11-15T00:00:00Z"},
                "rate_limited": False,
                "cooldown_until": None,
                "tiers": {},
            },
            "claude": {
                "enabled": True,
                "eligible_as_target": True,
                "target_exclusion_reason": None,
                "eligible_as_host_native": True,
                "host_native_exclusion_reason": None,
                "session_5h": {"used_pct": 15.0, "reset_at": None},
                "weekly_7d": {"used_pct": 30.0, "reset_at": None},
                "rate_limited": False,
                "cooldown_until": None,
                "tiers": {},
            },
            "agy": {
                "enabled": True,
                "eligible_as_target": True,
                "target_exclusion_reason": None,
                "eligible_as_host_native": True,
                "host_native_exclusion_reason": None,
                "session_5h": {"used_pct": None, "reset_at": None},
                "weekly_7d": {"used_pct": None, "reset_at": None},
                "rate_limited": False,
                "cooldown_until": None,
                "tiers": {},
            },
        },
        "routes": [
            {
                "route_id": "route-alpha000001",
                "decision_id": "dec-alpha-1",
                "status": "routed",
                "execution_mode": "cli",
                "repository": "/repos/pm-harness",
                "repository_id": "repo-pm",
                "host_agent": "codex",
                "target_agent": "claude",
                "model": "claude-opus-5-5",
                "qualified_model": "claude/claude-opus-5-5",
                "effort": "high",
                "task_type": "specified-implementation",
                "risk_class": "standard",
                "role": "Worker",
                "label": "#5609 Implement Project-scoped positions",
                "cap_met": True,
                "needs_review": False,
                "host_affinity_applied": False,
                "created_at": "2026-11-08T20:59:30Z",
                "observed_at": None,
                "outcome": None,
                "execution_id": None,
                "tokens": None,
                "seconds": None,
                "cost_usd": None,
                "invocation": {
                    "kind": "cli_command",
                    "argv": ["claude", "-p", "--model", "claude-opus-5-5", "--effort", "high"],
                },
                "forecasts": [
                    {"model": "claude/claude-opus-5-5", "effort": "high", "clean": 0.96, "loud": 0.03, "silent": 0.01}
                ],
                "note": None,
                "task_text": "#5609 Implement Project-scoped positions\nFull details here.",
            },
            {
                "route_id": "route-beta0000002",
                "decision_id": "dec-beta-2",
                "status": "review",
                "execution_mode": "cli",
                "repository": "/repos/pm-harness",
                "repository_id": "repo-pm",
                "host_agent": "codex",
                "target_agent": "agy",
                "model": "gemini-4-argon-high",
                "qualified_model": "agy/gemini-4-argon-high",
                "effort": "high",
                "task_type": "contract-change",
                "risk_class": "contract",
                "role": "Reviewer",
                "label": "#5610 Contract audit for billing seam",
                "cap_met": False,
                "needs_review": True,
                "host_affinity_applied": False,
                "created_at": "2026-11-08T20:55:00Z",
                "observed_at": None,
                "outcome": None,
                "execution_id": None,
                "tokens": None,
                "seconds": None,
                "cost_usd": None,
                "invocation": {
                    "kind": "cli_command",
                    "argv": ["agy", "-p", "--model", "gemini-4-argon-high", "--effort", "high"],
                },
                "forecasts": [],
                "note": None,
                "task_text": None,
            },
            {
                "route_id": "hh-gamma000003",
                "decision_id": "dec-gamma-3",
                "status": "clean",
                "execution_mode": "native",
                "repository": "/repos/agroute",
                "repository_id": "repo-agroute",
                "host_agent": "codex",
                "target_agent": "codex",
                "model": "gpt-6.1-terra",
                "qualified_model": "codex/gpt-6.1-terra",
                "effort": "medium",
                "task_type": "docs-settled-rules",
                "risk_class": "docs",
                "role": None,
                "label": "#11 Document TUI keybindings",
                "cap_met": True,
                "needs_review": False,
                "host_affinity_applied": False,
                "created_at": "2026-11-08T20:40:00Z",
                "observed_at": "2026-11-08T20:45:00Z",
                "outcome": "clean",
                "execution_id": "agroute-11-docs",
                "tokens": 18400,
                "seconds": 95.0,
                "cost_usd": 0.12,
                "invocation": None,
                "forecasts": [],
                "note": "Completed cleanly on first pass.",
                "task_text": None,
            },
        ],
    }


@pytest.mark.asyncio
async def test_tui_renders_routes_modals_and_filters():
    snap = _make_snapshot()
    app = build_app(lambda: deepcopy(snap), refresh_interval=60.0)

    async with app.run_test(size=(110, 28)) as pilot:
        await pilot.pause()

        subject = app.query_one("#route-subject", Static)
        assert "MACHINE │ AGROUTE │ 2 routed · 1 observed" in str(subject.render())

        table = app.query_one("#routes", DataTable)
        assert table.row_count == 3

        detail = app.query_one("#route-detail", Static)
        assert "route-alpha000001" in str(detail.render())
        assert "claude/claude-opus-5-5" in str(detail.render())

        # Toggle observed history off with 'h'
        await pilot.press("h")
        await pilot.pause()
        assert table.row_count == 2

        # Toggle observed history back on with 'h'
        await pilot.press("h")
        await pilot.pause()
        assert table.row_count == 3

        # Open route details modal with Enter and close with Escape
        await pilot.press("enter")
        await pilot.pause()
        show_body = app.screen.query_one("#show-body", Static)
        assert "Route ID: route-alpha000001" in str(show_body.render())
        assert "Forecast panel" in str(show_body.render())
        await pilot.press("escape")
        await pilot.pause()

        # Open live usage modal with 'u' and close
        await pilot.press("u")
        await pilot.pause()
        usage_body = app.screen.query_one("#show-body", Static)
        assert "[codex]" in str(usage_body.render())
        assert "42.0%" in str(usage_body.render())
        await pilot.press("q")
        await pilot.pause()

        # Open keys help modal with '?' and close
        await pilot.press("question_mark")
        await pilot.pause()
        keys_body = app.screen.query_one("#show-body", Static)
        assert "show selected route details" in str(keys_body.render())
        await pilot.press("enter")
        await pilot.pause()

        # Filter by target agent ('a') -> type 'agy' -> enter
        await pilot.press("a")
        await pilot.pause()
        filter_input = app.screen.query_one("#filter-query", Input)
        assert filter_input is not None
        await pilot.press("a", "g", "y")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert table.row_count == 1
        assert "route-beta0000002" in str(detail.render())

        # Reset agent filter ('a' -> enter on "All agents")
        await pilot.press("a")
        await pilot.pause()
        options = app.screen.query_one("#filter-options", OptionList)
        options.highlighted = 0
        await pilot.press("enter")
        await pilot.pause()
        assert table.row_count == 3


@pytest.mark.asyncio
async def test_tui_handles_snapshot_error_gracefully():
    def failing_factory():
        raise AgRouteError("state store is locked by another project")

    app = build_app(failing_factory, refresh_interval=60.0)
    async with app.run_test(size=(90, 24)) as pilot:
        await pilot.pause()
        subject = app.query_one("#route-subject", Static)
        assert "unavailable" in str(subject.render())
        detail = app.query_one("#route-detail", Static)
        assert "state store is locked" in str(detail.render())


def test_dynamic_discovery_and_howhard_correlation(tmp_path):
    # 1. Codex discovery reads visible models by priority without assuming model name prefixes
    codex_dir = tmp_path / "custom-codex"
    codex_dir.mkdir()
    (codex_dir / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "solaris-next-1",
                        "visibility": "list",
                        "priority": 1,
                        "default_reasoning_level": "medium",
                        "supported_reasoning_levels": [{"effort": "low"}, {"effort": "medium"}, {"effort": "high"}],
                    },
                    {
                        "slug": "codex-auto-review",
                        "visibility": "hide",
                        "priority": 2,
                        "supported_reasoning_levels": [{"effort": "medium"}],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    codex_cands = discover_codex_candidates(codex_home=codex_dir)
    assert [c["model"] for c in codex_cands] == ["solaris-next-1", "solaris-next-1", "solaris-next-1"]

    # 2. Claude discovery selects main section models and reads runtime.family dynamically
    claude_dir = tmp_path / "custom-claude"
    (claude_dir / "cache" / "model-catalog").mkdir(parents=True)
    (claude_dir / "cache" / "model-catalog" / "cat.json").write_text(
        json.dumps(
            {
                "version": 2,
                "catalog": {
                    "config": {
                        "models": [
                            {
                                "id": "claude-nova-6-0",
                                "short_name": "Nova",
                                "section": "main",
                                "runtime": {
                                    "family": "nova",
                                    "effort_levels": ["medium", "high", "xhigh"],
                                    "default_effort": "medium",
                                },
                            },
                            {
                                "id": "claude-opus-4-6",
                                "short_name": "Opus 4.6",
                                "section": "overflow",
                                "runtime": {"family": "opus", "effort_levels": ["high"]},
                            },
                        ]
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    claude_cands = discover_claude_candidates(claude_home=claude_dir)
    assert [c["model"] for c in claude_cands] == ["claude-nova-6-0", "claude-nova-6-0", "claude-nova-6-0"]
    assert all(c["tier"] == "nova" for c in claude_cands)

    # 3. Antigravity `agy models` parser strips ANSI spinners and parses all available models
    raw_agy = (
        "\x1b[?25l⠋ Fetching available models...\r"
        "gemini-4-argon-high       Gemini 4 Argon (High)\n"
        "gemini-3.8-flash-low      Gemini 3.8 Flash (Low)\n"
        "claude-opus-5-5-high      Claude Opus 5.5 (High)\n"
    )
    agy_cands = parse_agy_models_output(raw_agy)
    assert agy_cands == [
        {"model": "gemini-4-argon-high", "effort": "high", "tier": "pro", "display_name": "Gemini 4 Argon (High)"},
        {"model": "gemini-3.8-flash-low", "effort": "low", "tier": "flash", "display_name": "Gemini 3.8 Flash (Low)"},
        {"model": "claude-opus-5-5-high", "effort": "high", "tier": "pro", "display_name": "Claude Opus 5.5 (High)"},
    ]

    # 4. End-to-end correlation between agroute plan, howhard decisions, and howhard pending.jsonl
    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    hh_dir = git_dir / "howhard"
    (hh_dir / "decisions").mkdir(parents=True)
    state_dir = git_dir / "agroute"

    dec_1 = "1111111111111111111111111111111111111111111111111111111111111111"
    dec_2 = "2222222222222222222222222222222222222222222222222222222222222222"
    (hh_dir / "decision-events.jsonl").write_text(
        json.dumps({"at_utc": "2026-11-08T18:00:00+00:00", "decision_id": dec_1, "task_type": "task"})
        + "\n"
        + json.dumps({"at_utc": "2026-11-08T19:00:00+00:00", "decision_id": dec_2, "task_type": "task"})
        + "\n",
        encoding="utf-8",
    )
    (hh_dir / "decisions" / f"{dec_1}.json").write_text(
        json.dumps(
            {
                "chosen": {"model": "claude/claude-sonnet-4-6", "effort": "high"},
                "task_type": "specified-implementation",
                "risk_class": "standard",
                "context": {"task_text": "#101 Build feature X\nBody text"},
            }
        ),
        encoding="utf-8",
    )
    (hh_dir / "decisions" / f"{dec_2}.json").write_text(
        json.dumps(
            {
                "chosen": {"model": "gpt-5.4", "effort": "high"},
                "task_type": "guarded-landing",
                "risk_class": "standard",
                "context": {"task_text": "#102 Land feature X"},
            }
        ),
        encoding="utf-8",
    )
    (hh_dir / "pending.jsonl").write_text(
        json.dumps(
            {
                "id": "run-101",
                "decision_id": dec_1,
                "observed_at": "2026-11-08T18:30:00+00:00",
                "outcome": "loud",
                "model": "claude/claude-sonnet-4-6",
                "effort": "high",
                "task_type": "specified-implementation",
                "risk_class": "standard",
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "run-101-amended",
                "amends": "run-101",
                "observed_at": "2026-11-08T18:45:00+00:00",
                "outcome": "clean",
                "model": "claude/claude-sonnet-4-6",
                "effort": "high",
                "tokens": 12000,
                "seconds": 45.0,
                "cost_usd": 0.08,
                "note": "Amended to clean after verification.",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    router = Router(project=repo, state_directory=state_dir)
    plan = router.plan(
        "agy",
        {
            "decision_id": dec_1,
            "chosen": {"model": "claude/claude-sonnet-4-6", "effort": "high"},
            "cap_met": True,
            "needs_review": False,
        },
        prompt="#101 Build feature X",
        now="2026-11-08T18:00:05Z",
    )
    snap = router.snapshot(now="2026-11-08T20:00:00Z")
    assert snap["routed_count"] == 1
    assert snap["observed_count"] == 1
    assert len(snap["routes"]) == 2

    r_dec1 = router.show_route(plan["route_id"], now="2026-11-08T20:00:00Z")
    assert r_dec1["status"] == "clean"
    assert r_dec1["label"] == "#101 Build feature X"
    assert r_dec1["tokens"] == 12000

    r_dec2 = router.show_route(dec_2[:10], now="2026-11-08T20:00:00Z")
    assert r_dec2["status"] == "routed"
    assert r_dec2["target_agent"] == "codex"
    assert r_dec2["label"] == "#102 Land feature X"

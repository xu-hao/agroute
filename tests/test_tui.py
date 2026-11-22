"""Real-Textual behavioral tests for the realtime model-route view (agr tui)."""

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
                "repository": "example-project",
                "repository_id": "repo-example",
                "host_agent": "codex",
                "target_agent": "claude",
                "model": "claude-opus-5-5",
                "qualified_model": "claude/claude-opus-5-5",
                "effort": "high",
                "task_type": "specified-implementation",
                "risk_class": "standard",
                "role": "Worker",
                "label": "Implement example feature",
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
                "session_cost_usd": 0.22,
                "needed_credit_usd": 0.3,
                "credit_budget": {"available_credit_usd": 1.5},
                "cost_per_credit": 0.75,
                "monthly_cost_usd": 20.0,
                "weekly_cost_usd": 20.0 * 7.0 / 30.0,
                "invocation": {
                    "kind": "cli_command",
                    "argv": ["claude", "-p", "--model", "claude-opus-5-5", "--effort", "high"],
                },
                "forecasts": [
                    {"model": "claude/claude-opus-5-5", "effort": "high", "clean": 0.96, "loud": 0.03, "silent": 0.01}
                ],
                "note": None,
                "task_text": "Implement example feature\nFull details here.",
            },
            {
                "route_id": "route-beta0000002",
                "decision_id": "dec-beta-2",
                "status": "review",
                "execution_mode": "cli",
                "repository": "example-project",
                "repository_id": "repo-example",
                "host_agent": "codex",
                "target_agent": "agy",
                "model": "gemini-4-argon-high",
                "qualified_model": "agy/gemini-4-argon-high",
                "effort": "high",
                "task_type": "contract-change",
                "risk_class": "contract",
                "role": "Reviewer",
                "label": "Review example API contract",
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
        assert "MACHINE │ AGROUTE │ [ROUTES] · quotas (u) │ 2 routed · 1 observed" in str(subject.render())
        assert len(app.query("#quota-strip")) == 0
        assert len(app.query("#quota-rule")) == 0

        table = app.query_one("#routes", DataTable)
        quotas = app.query_one("#quotas", DataTable)
        assert table.display is True
        assert quotas.display is False
        assert app.focused is table
        assert table.row_count == 3

        status = str(app.query_one("#route-status", Static).render())
        assert "codex OK · claude OK · agy OK · showing 3/3 routes" in status

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
        assert "Session cost USD: 0.22" in str(show_body.render())
        assert "Needed credit USD: 0.3" in str(show_body.render())
        assert "Available credit USD: 1.5" in str(show_body.render())
        assert "Cost per credit: 0.75" in str(show_body.render())
        assert "Monthly subscription cost USD: 20.0" in str(show_body.render())
        assert "Observed cost USD: —" in str(show_body.render())
        await pilot.press("escape")
        await pilot.pause()

        # Switch to QUOTAS view with 'u', open full breakdown modal with Enter, close modal, and return to ROUTES with Tab
        await pilot.press("u")
        await pilot.pause()
        assert table.display is False
        assert quotas.display is True
        assert app.focused is quotas
        assert "MACHINE │ AGROUTE │ routes (u) · [QUOTAS] │ role filter: all (H)" in str(subject.render())
        assert [str(col.label) for col in quotas.ordered_columns] == [
            "POOL",
            "PLAN",
            "ROLE",
            "STATUS",
            "WIN",
            "%USED",
            "%RSV(nom)",
            "%RSV(eff)",
            "%LEFT",
            "$CAP",
            "$USED",
            "$RSV(nom)",
            "$RSV(eff)",
            "$LEFT",
            "RESETS",
        ]
        # Default "all" orders rows left-to-right by POOL -> ROLE -> WIN (target 5h/7d then host 5h/7d per pool)
        assert [str(r.key.value) for r in quotas.ordered_rows] == [
            "codex:shared:5h:target",
            "codex:shared:7d:target",
            "codex:shared:5h:host",
            "codex:shared:7d:host",
            "claude:shared:5h:target",
            "claude:shared:7d:target",
            "claude:shared:5h:host",
            "claude:shared:7d:host",
            "agy:shared:5h:target",
            "agy:shared:7d:target",
            "agy:shared:5h:host",
            "agy:shared:7d:host",
        ]
        codex_5h_t = [c.plain for c in quotas.get_row("codex:shared:5h:target")]
        codex_5h_h = [c.plain for c in quotas.get_row("codex:shared:5h:host")]
        assert codex_5h_t[:14] == [
            "codex", "—", "target", "OK", "5h", "42%", "10%", "10%", "48%", "—", "—", "—", "—", "—"
        ]
        assert codex_5h_h[:14] == [
            "codex", "—", "host", "OK", "5h", "42%", "10%", "10%", "48%", "—", "—", "—", "—", "—"
        ]
        assert "(Enter for full breakdown)" in str(detail.render())

        await pilot.press("enter")
        await pilot.pause()
        usage_body = app.screen.query_one("#show-body", Static)
        assert "[codex]" in str(usage_body.render())
        assert "42.0%" in str(usage_body.render())
        await pilot.press("q")
        await pilot.pause()
        assert quotas.display is True
        assert app.focused is quotas

        await pilot.press("tab")
        await pilot.pause()
        assert table.display is True
        assert quotas.display is False
        assert app.focused is table

        # Open keys help modal with '?' and close
        await pilot.press("question_mark")
        await pilot.pause()
        keys_body = app.screen.query_one("#show-body", Static)
        assert "show selected route details" in str(keys_body.render())
        assert "toggle routes / quotas view" in str(keys_body.render())
        assert "cycle quota role filter" in str(keys_body.render())
        await pilot.press("enter")
        await pilot.pause()

        # Filter by target agent ('a') -> verify Tab inside modal moves focus without switching view -> type 'agy' -> enter
        await pilot.press("a")
        await pilot.pause()
        filter_input = app.screen.query_one("#filter-query", Input)
        assert filter_input is not None
        await pilot.press("tab")
        await pilot.pause()
        assert table.display is True
        assert quotas.display is False
        filter_input.focus()
        await pilot.pause()
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

    # 4. End-to-end correlation between agr plan, howhard decisions, and howhard pending.jsonl
    from conftest import write_howhard_payload_decision

    repo = tmp_path / "repo"
    git_dir = repo / ".git"
    hh_dir = git_dir / "howhard"
    hh_dir.mkdir(parents=True)
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
    write_howhard_payload_decision(
        hh_dir,
        dec_1,
        {
            "chosen": {"model": "claude/claude-sonnet-4-6", "effort": "high"},
            "task_type": "specified-implementation",
            "risk_class": "standard",
            "context": {"task_text": "#101 Build feature X\nBody text"},
        },
        created_at="2026-11-08T18:00:00+00:00",
    )
    write_howhard_payload_decision(
        hh_dir,
        dec_2,
        {
            "chosen": {"model": "gpt-5.4", "effort": "high"},
            "task_type": "guarded-landing",
            "risk_class": "standard",
            "context": {"task_text": "#102 Land feature X"},
        },
        created_at="2026-11-08T19:00:00+00:00",
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


@pytest.mark.asyncio
async def test_tui_automatically_probes_and_manual_probe_refreshes():
    calls = []
    def probe(manual):
        calls.append(manual)
    app = build_app(lambda: deepcopy(_make_snapshot()), probe_factory=probe, refresh_interval=60)
    async with app.run_test(size=(100, 24)) as pilot:
        await pilot.pause()
        assert calls == [False]
        await pilot.press('P')
        await pilot.pause()
        assert calls == [False, True]


@pytest.mark.asyncio
async def test_probe_single_flight_cooldown_and_manual_bypass():
    import threading

    entered = threading.Event()
    release = threading.Event()
    calls = []

    def probe(manual):
        calls.append(manual)
        entered.set()
        release.wait(5)
        raise ValueError('offline')

    app = build_app(lambda: deepcopy(_make_snapshot()), probe_factory=probe, refresh_interval=60)
    try:
        async with app.run_test(size=(100, 24)) as pilot:
            await pilot.pause()
            assert entered.is_set()
            await pilot.press('P')
            app.action_refresh()
            await pilot.pause()
            assert calls == [False]
            release.set()
            await pilot.pause()
            app.action_refresh()
            await pilot.pause()
            assert calls == [False]
            await pilot.press('P')
            await pilot.pause()
            assert calls == [False, True]
    finally:
        release.set()


def test_cli_tui_wires_probe_factory(monkeypatch):
    from agroute import cli

    calls = []
    class FakeRouter:
        def __init__(self, *args, **kwargs):
            pass
        def tui_probe(self, manual, *, now):
            calls.append((manual, now))
    def fake_run(snapshot, **kwargs):
        kwargs['probe_factory'](False)
        kwargs['probe_factory'](True)
        return 0
    monkeypatch.setattr(cli, 'Router', FakeRouter)
    monkeypatch.setattr(cli.tui, 'run', fake_run)
    assert cli.main(['tui', '--now', '2026-11-09T12:00:00Z']) == 0
    assert calls == [(False, '2026-11-09T12:00:00Z'), (True, '2026-11-09T12:00:00Z')]


@pytest.mark.asyncio
async def test_tui_shows_estimated_credit_alongside_percentages():
    snap = _make_snapshot()
    snap["agents"]["codex"]["effective_plan"] = "plus-20"
    snap["agents"]["codex"]["credit_calibration"] = {
        "shared": {
            "plan": "plus-20",
            "available_credit_usd": 1.6,
            "session_5h": {
                "pool_group": "shared",
                "calibrated_credit_usd": 20.0,
                "used_pct": 42.0,
                "remaining_credit_usd": 1.6,
                "reserve_credit_usd": 10.0,
            },
            "weekly_7d": {
                "pool_group": "shared",
                "calibrated_credit_usd": 50.0,
                "used_pct": 18.0,
                "remaining_credit_usd": 31.0,
                "reserve_credit_usd": 10.0,
            },
        }
    }
    snap["agents"]["codex"]["models"] = {
        "gpt-6.2-sol": {
            "quota_group": "shared",
            "session_5h": {"used_pct": 42.0},
            "weekly_7d": {"used_pct": 18.0},
            "eligible": True,
            "cost_per_credit": 0.0933,
            "credit_budget": {
                "available_credit_usd": 1.6,
                "session_5h_remaining_credit_usd": 1.6,
                "weekly_7d_remaining_credit_usd": 31.0,
            },
        }
    }
    snap["agents"]["claude"]["effective_plan"] = "max-20x"
    snap["agents"]["claude"]["session_5h"] = {"used_pct": None, "reset_at": None}
    snap["agents"]["claude"]["credit_calibration"] = {
        "shared": {
            "plan": "max-20x",
            "available_credit_usd": 620.0,
            "session_5h": {
                "pool_group": "shared",
                "calibrated_credit_usd": 360.0,
                "used_pct": None,
                "remaining_credit_usd": None,
                "reserve_credit_usd": 10.0,
            },
            "weekly_7d": {
                "pool_group": "shared",
                "calibrated_credit_usd": 900.0,
                "used_pct": 30.0,
                "remaining_credit_usd": 620.0,
                "reserve_credit_usd": 10.0,
            },
        },
        "fable": {
            "plan": "max-20x",
            "available_credit_usd": 200.0,
            "weekly_7d": {
                "pool_group": "fable",
                "calibrated_credit_usd": 300.0,
                "used_pct": 30.0,
                "remaining_credit_usd": 200.0,
                "reserve_credit_usd": 10.0,
            },
        },
    }
    snap["agents"]["agy"]["effective_plan"] = "ultra-200"
    snap["agents"]["agy"]["session_5h"] = {"used_pct": 5.44, "reset_at": "2026-11-09T02:00:00Z"}
    snap["agents"]["agy"]["weekly_7d"] = {"used_pct": 16.73, "reset_at": "2026-11-15T00:00:00Z"}
    snap["agents"]["agy"]["credit_calibration"] = {
        "3p": {
            "plan": "ultra-200",
            "available_credit_usd": 15.0,
            "session_5h": {
                "pool_group": "3p",
                "calibrated_credit_usd": 25.0,
                "used_pct": 0.0,
                "remaining_credit_usd": 15.0,
                "reserve_credit_usd": 10.0,
            },
            "weekly_7d": {
                "pool_group": "3p",
                "calibrated_credit_usd": 50.0,
                "used_pct": 6.25,
                "remaining_credit_usd": 36.875,
                "reserve_credit_usd": 10.0,
            },
        },
        "gemini": {
            "plan": "ultra-200",
            "available_credit_usd": 292.592,
            "session_5h": {
                "pool_group": "gemini",
                "calibrated_credit_usd": 320.0,
                "used_pct": 5.44,
                "remaining_credit_usd": 292.592,
                "reserve_credit_usd": 10.0,
            },
            "weekly_7d": {
                "pool_group": "gemini",
                "calibrated_credit_usd": 1350.0,
                "used_pct": 16.73,
                "remaining_credit_usd": 1114.145,
                "reserve_credit_usd": 10.0,
            },
        },
    }

    app = build_app(lambda: deepcopy(snap), refresh_interval=60.0)
    async with app.run_test(size=(140, 32)) as pilot:
        await pilot.pause()

        routes_table = app.query_one("#routes", DataTable)
        quotas_table = app.query_one("#quotas", DataTable)
        assert routes_table.display is True
        assert quotas_table.display is False

        status = str(app.query_one("#route-status", Static).render())
        assert "codex OK · claude OK · agy OK · showing 3/3 routes" in status

        # Switch to QUOTAS view via 'u'
        await pilot.press("u")
        await pilot.pause()
        assert routes_table.display is False
        assert quotas_table.display is True
        assert app.focused is quotas_table

        col_labels = [str(col.label) for col in quotas_table.ordered_columns]
        assert col_labels == [
            "POOL",
            "PLAN",
            "ROLE",
            "STATUS",
            "WIN",
            "%USED",
            "%RSV(nom)",
            "%RSV(eff)",
            "%LEFT",
            "$CAP",
            "$USED",
            "$RSV(nom)",
            "$RSV(eff)",
            "$LEFT",
            "RESETS",
        ]
        # Default role filter "all": rows are ordered left-to-right by POOL -> ROLE -> WIN
        # (within each pool, target 5h/7d rows precede host 5h/7d rows without '*' on POOL).
        ordered_keys = [str(r.key.value) for r in quotas_table.ordered_rows]
        expected_all_keys = [
            "codex:shared:5h:target",
            "codex:shared:7d:target",
            "codex:shared:5h:host",
            "codex:shared:7d:host",
            "claude:shared:5h:target",
            "claude:shared:7d:target",
            "claude:shared:5h:host",
            "claude:shared:7d:host",
            "claude:fable:7d:target",
            "claude:fable:7d:host",
            "agy:gemini:5h:target",
            "agy:gemini:7d:target",
            "agy:gemini:5h:host",
            "agy:gemini:7d:host",
            "agy:3p:5h:target",
            "agy:3p:7d:target",
            "agy:3p:5h:host",
            "agy:3p:7d:host",
        ]
        assert ordered_keys == expected_all_keys
        assert [
            (
                quotas_table.get_row(k)[0].plain,
                quotas_table.get_row(k)[2].plain,
                quotas_table.get_row(k)[4].plain,
            )
            for k in ordered_keys
        ] == [
            ("codex", "target", "5h"),
            ("codex", "target", "7d"),
            ("codex", "host", "5h"),
            ("codex", "host", "7d"),
            ("claude/shared", "target", "5h"),
            ("claude/shared", "target", "7d"),
            ("claude/shared", "host", "5h"),
            ("claude/shared", "host", "7d"),
            ("claude/fable", "target", "7d"),
            ("claude/fable", "host", "7d"),
            ("agy/gemini", "target", "5h"),
            ("agy/gemini", "target", "7d"),
            ("agy/gemini", "host", "5h"),
            ("agy/gemini", "host", "7d"),
            ("agy/3p", "target", "5h"),
            ("agy/3p", "target", "7d"),
            ("agy/3p", "host", "5h"),
            ("agy/3p", "host", "7d"),
        ]
        row_5h = quotas_table.get_row("codex:shared:5h:target")
        row_7d = quotas_table.get_row("codex:shared:7d:target")
        assert [c.plain for c in row_5h[:5]] == ["codex", "plus-20", "target", "OK", "5h"]
        assert [c.plain for c in row_7d[:5]] == ["codex", "plus-20", "target", "OK", "7d"]
        assert row_5h[3].style == "bold green"
        # 5h: 42% used, 10% nom rsv, $10/$20 = 50% eff rsv -> 8% left, $8.40 used, $10 nom, $10 eff, $1.60 left
        assert [c.plain for c in row_5h[5:14]] == [
            "42%", "10%", "50%", "8%", "$20", "$8.40", "$10", "$10", "$1.60"
        ]
        assert [c.plain for c in row_7d[5:14]] == [
            "18%", "5%", "20%", "62%", "$50", "$9", "$10", "$10", "$31"
        ]

        claude_shared_7d = quotas_table.get_row("claude:shared:7d:target")
        claude_fable_7d = quotas_table.get_row("claude:fable:7d:target")
        assert claude_shared_7d[0].plain == "claude/shared" and claude_shared_7d[1].plain == "max-20x" and claude_shared_7d[13].plain == "$585"
        assert claude_fable_7d[0].plain == "claude/fable" and claude_fable_7d[13].plain == "$195"

        agy_gem_5h_t = quotas_table.get_row("agy:gemini:5h:target")
        agy_gem_5h_h = quotas_table.get_row("agy:gemini:5h:host")
        agy_3p_5h_h = quotas_table.get_row("agy:3p:5h:host")
        assert agy_gem_5h_t[0].plain == "agy/gemini" and agy_gem_5h_t[2].plain == "target"
        assert agy_gem_5h_h[0].plain == "agy/gemini" and agy_gem_5h_h[2].plain == "host"
        assert agy_3p_5h_h[0].plain == "agy/3p" and agy_3p_5h_h[2].plain == "host"

        # Detail pane shows 6-line summary for highlighted codex pool row with [pool_name · role]
        detail_codex = str(app.query_one("#route-detail", Static).render())
        assert len(detail_codex.splitlines()) == 6
        assert "codex [shared · target] · plan: plus-20" in detail_codex and "status: OK" in detail_codex
        assert "target_eligible: True" in detail_codex
        assert "host_native_eligible: True" in detail_codex
        assert "reserves (5h target): nominal 10% / $10.00 -> effective 50% / $10.00" in detail_codex
        assert "5h session: 42%" in detail_codex and "reset:" in detail_codex
        assert "7d weekly:  18%" in detail_codex and "reset:" in detail_codex
        assert "models (shared): 1/1 eligible" in detail_codex and "(Enter for full breakdown)" in detail_codex

        # Moving cursor down to row 4 (claude:shared:5h:target) updates #route-detail to the highlighted pool
        quotas_table.move_cursor(row=4)
        await pilot.pause()
        detail_claude = str(app.query_one("#route-detail", Static).render())
        assert "claude/shared [shared · target] · plan: max-20x" in detail_claude

        # Pressing 'H' cycles role filter across all -> target -> host -> agy -> claude -> codex -> all
        expected_cycles = [
            (
                "target",
                [
                    "codex:shared:5h:target",
                    "codex:shared:7d:target",
                    "claude:shared:5h:target",
                    "claude:shared:7d:target",
                    "claude:fable:7d:target",
                    "agy:gemini:5h:target",
                    "agy:gemini:7d:target",
                    "agy:3p:5h:target",
                    "agy:3p:7d:target",
                ],
            ),
            (
                "host",
                [
                    "codex:shared:5h:host",
                    "codex:shared:7d:host",
                    "claude:shared:5h:host",
                    "claude:shared:7d:host",
                    "claude:fable:7d:host",
                    "agy:gemini:5h:host",
                    "agy:gemini:7d:host",
                    "agy:3p:5h:host",
                    "agy:3p:7d:host",
                ],
            ),
            (
                "agy",
                [
                    "codex:shared:5h:target",
                    "codex:shared:7d:target",
                    "claude:shared:5h:target",
                    "claude:shared:7d:target",
                    "claude:fable:7d:target",
                    "agy:gemini:5h:host",
                    "agy:gemini:7d:host",
                    "agy:3p:5h:host",
                    "agy:3p:7d:host",
                ],
            ),
            (
                "claude",
                [
                    "codex:shared:5h:target",
                    "codex:shared:7d:target",
                    "claude:shared:5h:host",
                    "claude:shared:7d:host",
                    "claude:fable:7d:host",
                    "agy:gemini:5h:target",
                    "agy:gemini:7d:target",
                    "agy:3p:5h:target",
                    "agy:3p:7d:target",
                ],
            ),
            (
                "codex",
                [
                    "codex:shared:5h:host",
                    "codex:shared:7d:host",
                    "claude:shared:5h:target",
                    "claude:shared:7d:target",
                    "claude:fable:7d:target",
                    "agy:gemini:5h:target",
                    "agy:gemini:7d:target",
                    "agy:3p:5h:target",
                    "agy:3p:7d:target",
                ],
            ),
            ("all", expected_all_keys),
        ]
        for expected_role_filter, expected_keys in expected_cycles:
            await pilot.press("H")
            await pilot.pause()
            subj = str(app.query_one("#route-subject", Static).render())
            assert f"role filter: {expected_role_filter} (H)" in subj
            keys_now = [str(r.key.value) for r in quotas_table.ordered_rows]
            assert keys_now == expected_keys

        # Pressing Enter in #quotas opens the full Agent Usage & Eligibility modal
        await pilot.press("enter")
        await pilot.pause()
        usage_body = str(app.screen.query_one("#show-body", Static).render())
        # Agent top-level 5h/7d lines include estimated credit used/ceiling/left/reserve.
        assert "5h session used: 42.0% (~$8.40/$20.00 credit, ~$1.60 left, $10.00 reserve)" in usage_body
        assert "7d weekly used:  18.0% (~$9.00/$50.00 credit, ~$31.00 left, $10.00 reserve)" in usage_body
        assert "5h session used: —% (est. ceiling ~$360.00 credit, $10.00 reserve)" in usage_body
        assert "5h session used: 5.44% (~$17.41/$320.00 credit, ~$292.59 left, $10.00 reserve)" in usage_body
        # Multi-pool summaries show all calibrated pools (including claude fable and agy 3p/gemini).
        assert "Estimated credit pools (plan: max-20x):" in usage_body
        assert "fable: available=~$200.00 credit" in usage_body
        assert "Estimated credit pools (plan: ultra-200):" in usage_body
        assert "3p: available=~$15.00 credit" in usage_body
        assert "gemini: available=~$292.59 credit" in usage_body
        # Per-model quota shows estimated remaining credit alongside 5h and 7d percentages.
        assert "codex/gpt-6.2-sol [shared]: 5h=42.0% (~$1.60 left) 7d=18.0% (~$31.00 left)" in usage_body
        await pilot.press("q")
        await pilot.pause()
        assert quotas_table.display is True
        assert app.focused is quotas_table

        # Pressing 'u' again returns to ROUTES view
        await pilot.press("u")
        await pilot.pause()
        assert routes_table.display is True
        assert quotas_table.display is False
        assert app.focused is routes_table


@pytest.mark.asyncio
async def test_tui_end_to_end_router_snapshot_renders_calibrated_credit(tmp_path):
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        """
[agents.codex]
plan = "plus-20"
min_remaining_credit_usd = 5.0

[[agents.codex.cli_candidates]]
model = "gpt-6.2-sol"
effort = "high"
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=tmp_path, state_directory=tmp_path / "state", include_user_config=False)
    router.usage_set(
        "codex",
        session_used_pct=25.0,
        weekly_used_pct=40.0,
        now="2026-11-20T12:00:00Z",
    )

    app = build_app(lambda: router.snapshot(all_repos=False, now="2026-11-20T12:00:00Z"), refresh_interval=60.0)
    async with app.run_test(size=(130, 30)) as pilot:
        await pilot.pause()
        await pilot.press("u")
        await pilot.pause()
        quotas_table = app.query_one("#quotas", DataTable)
        assert quotas_table.display is True
        # plus-20 defaults: 5h=$20 (25% used -> $5 used, $5 nom/$5 eff rsv, $10 left), 7d=$50 (40% used -> $20 used, $5 nom/$5 eff rsv, $25 left)
        row_5h = [c.plain for c in quotas_table.get_row("codex:shared:5h:target")]
        row_7d = [c.plain for c in quotas_table.get_row("codex:shared:7d:target")]
        assert row_5h[:5] == ["codex", "plus-20", "target", "OK", "5h"]
        assert row_5h[5:14] == ["25%", "10%", "25%", "50%", "$20", "$5", "$5", "$5", "$10"]
        assert row_7d[:5] == ["codex", "plus-20", "target", "OK", "7d"]
        assert row_7d[5:14] == ["40%", "5%", "10%", "50%", "$50", "$20", "$5", "$5", "$25"]

        await pilot.press("enter")
        await pilot.pause()
        usage_body = str(app.screen.query_one("#show-body", Static).render())
        assert "5h session used: 25.0% (~$5.00/$20.00 credit, ~$10.00 left, $5.00 reserve)" in usage_body
        assert "7d weekly used:  40.0% (~$20.00/$50.00 credit, ~$25.00 left, $5.00 reserve)" in usage_body
        assert "Estimated credit pools (plan: plus-20):" in usage_body
        assert "codex/gpt-6.2-sol [shared]: 5h=25.0% (~$10.00 left) 7d=40.0% (~$25.00 left)" in usage_body

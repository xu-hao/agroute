"""Behavioral tests for usage_show agent eligibility under credit reserves and reservations (#89)."""

from pathlib import Path

import pytest

from agroute import Router
from agroute.tui import _agent_quota_badge, _fmt_credit


def test_usage_show_marks_target_and_host_ineligible_when_post_reserve_credit_exhausted(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = repo / "agroute.toml"
    cfg.write_text(
        """
[routing]
default_host = "agy"

[agents.codex]
plan = "plus-20"
min_remaining_credit_usd = 10.0
host_reserve_credit_usd = 25.0
host_reserve_pct = 5.0
max_session_used_pct = 95.0
max_weekly_used_pct = 95.0
native_candidates = [{ model = "gpt-5.4", effort = "high" }]
cli_candidates = [{ model = "gpt-5.4", effort = "high" }]

[agents.claude]
enabled = false
[agents.agy]
enabled = false
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=repo, state_directory=repo / ".agroute", include_user_config=False)
    # plus-20 weekly prior ceiling is $50:
    # At 81% weekly used ($9.50 raw left < $10 min_remaining_credit_usd), percentage is < 95%,
    # yet post-reserve weekly credit is $0 -> both target and host_native must be False!
    router.usage_set(
        "codex",
        session_used_pct=0.0,
        session_reset_at="2026-11-22T10:00:00Z",
        weekly_used_pct=81.0,
        weekly_reset_at="2026-11-27T05:00:00Z",
        now="2026-11-22T05:00:00Z",
    )
    snap = router.usage_show("codex", now="2026-11-22T05:01:00Z")
    codex = snap["agents"]["codex"]
    assert codex["eligible_as_target"] is False
    assert "weekly_7d credit_budget exhausted" in (codex["target_exclusion_reason"] or "")
    assert codex["eligible_as_host_native"] is False
    # Host reserve is $10 + $25 = $35, which exhausts both the $20 5h cap and $9.50 raw left on 7d.
    assert "credit_budget exhausted (0 USD remaining after 35 USD reserve)" in (
        codex["host_native_exclusion_reason"] or ""
    )
    assert _agent_quota_badge("codex", codex) == "codex lim"


def test_usage_show_distinguishes_host_reserve_from_target_reserve(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = repo / "agroute.toml"
    cfg.write_text(
        """
[routing]
default_host = "codex"

[agents.codex]
plan = "plus-20"
min_remaining_credit_usd = 5.0
host_reserve_credit_usd = 12.0
native_candidates = [{ model = "gpt-5.4", effort = "high" }]
cli_candidates = [{ model = "gpt-5.4", effort = "high" }]
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=repo, state_directory=repo / ".agroute", include_user_config=False)
    # plus-20: 5h=$20, 7d=$50. At 0% 5h ($20 raw left) and 70% weekly used ($15 raw left):
    # - As cross-agent target ($5 reserve -> $15 5h left, $10 7d left): eligible_as_target is True!
    # - As host root ($5 + $12 = $17 reserve -> $3 5h left, $0 7d left): eligible_as_host_native is False!
    router.usage_set(
        "codex",
        session_used_pct=0.0,
        session_reset_at="2026-11-22T10:00:00Z",
        weekly_used_pct=70.0,
        weekly_reset_at="2026-11-27T05:00:00Z",
        now="2026-11-22T05:00:00Z",
    )
    codex = router.usage_show("codex", now="2026-11-22T05:01:00Z")["agents"]["codex"]
    assert codex["eligible_as_target"] is True
    assert codex["target_exclusion_reason"] is None
    assert codex["eligible_as_host_native"] is False
    assert "weekly_7d credit_budget exhausted (0 USD remaining after 17 USD reserve)" in (
        codex["host_native_exclusion_reason"] or ""
    )


def test_usage_show_marks_ineligible_when_inflight_reservation_exhausts_remaining_credit(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = repo / "agroute.toml"
    cfg.write_text(
        """
[routing]
default_host = "agy"

[agents.codex]
plan = "plus-20"
min_remaining_credit_usd = 10.0
cli_candidates = [{ model = "gpt-5.4", effort = "high" }]
native_candidates = [{ model = "gpt-5.4", effort = "high" }]

[agents.agy]
native_candidates = [{ model = "gemini-4-carbon-high", effort = "high" }]
cli_candidates = [{ model = "gemini-4-carbon-high", effort = "high" }]
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=repo, state_directory=repo / ".agroute", include_user_config=False)
    router.usage_set("agy", session_used_pct=5.0, weekly_used_pct=5.0, now="2026-11-22T05:00:00Z")
    # 76% weekly used on $50 -> $12 raw left -> $2 left after $10 reserve
    router.usage_set(
        "codex",
        session_used_pct=0.0,
        session_reset_at="2026-11-22T10:00:00Z",
        weekly_used_pct=76.0,
        weekly_reset_at="2026-11-27T05:00:00Z",
        now="2026-11-22T05:00:00Z",
    )
    assert router.usage_show("codex", now="2026-11-22T05:01:00Z")["agents"]["codex"]["eligible_as_target"] is True

    # Plan a $2.0 credit route -> exhausts the remaining $2 in-flight!
    router.plan(
        "agy",
        {
            "format": "howhard-decision-v9",
            "decision_id": "a" * 64,
            "chosen": {"model": "codex/gpt-5.4", "effort": "high"},
            "cells": [{"model": "codex/gpt-5.4", "effort": "high", "needed_credit_usd": 2.0, "session_cost_usd": 0.2}],
        },
        prompt="Exhaust remaining $2 via reservation",
        now="2026-11-22T05:01:00Z",
    )
    after = router.usage_show("codex", now="2026-11-22T05:02:00Z")["agents"]["codex"]
    assert after["eligible_as_target"] is False
    assert "reserved" in (after["target_exclusion_reason"] or "")


def test_compact_zero_and_whole_dollar_formatting():
    assert _fmt_credit(0.0, compact=True) == "$0"
    assert _fmt_credit(5.0, compact=True) == "$5"
    assert _fmt_credit(4.5, compact=True) == "$4.50"
    assert _fmt_credit(10.0, compact=True) == "$10"


@pytest.mark.asyncio
async def test_tui_host_aware_quota_ledger_and_host_cycle(tmp_path: Path):
    pytest.importorskip("textual")
    from textual.widgets import Static
    from agroute.tui import build_app

    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = repo / "agroute.toml"
    cfg.write_text(
        """
[routing]
default_host = "agy"

[agents.codex]
plan = "plus-20"
min_remaining_credit_usd = 10.0
host_reserve_credit_usd = 25.0
host_reserve_pct = 5.0
max_session_used_pct = 95.0
max_weekly_used_pct = 95.0
cli_candidates = [{ model = "gpt-5.4", effort = "high" }]
native_candidates = [{ model = "gpt-5.4", effort = "high" }]

[agents.claude]
plan = "max-20x"
min_remaining_credit_usd = 10.0
host_reserve_credit_usd = 25.0
host_reserve_pct = 5.0
max_session_used_pct = 95.0
max_weekly_used_pct = 95.0
max_tier_used_pct = 95.0
cli_candidates = [{ model = "claude-sonnet-4-6", effort = "high" }]
native_candidates = [{ model = "claude-sonnet-4-6", effort = "high" }]

[agents.agy]
plan = "ultra-200"
min_remaining_credit_usd = 10.0
host_reserve_credit_usd = 25.0
host_reserve_pct = 5.0
max_session_used_pct = 95.0
max_weekly_used_pct = 95.0
max_tier_used_pct = 95.0
cli_candidates = [
    { model = "gemini-4-carbon-high", effort = "high", tier = "pro" },
    { model = "claude-opus-5-5-high", effort = "high", tier = "pro" },
]
native_candidates = [
    { model = "gemini-4-carbon-high", effort = "high", tier = "pro" },
    { model = "claude-opus-5-5-high", effort = "high", tier = "pro" },
]
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=repo, state_directory=repo / ".agroute", include_user_config=False)
    now = "2026-11-22T06:00:00Z"
    router.usage_set(
        "codex",
        session_used_pct=0.0,
        session_reset_at="2026-11-22T10:48:00Z",
        weekly_used_pct=81.0,
        weekly_reset_at="2026-11-27T05:00:00Z",
        now=now,
    )
    router.usage_set(
        "claude",
        weekly_used_pct=100.0,
        weekly_reset_at="2026-11-24T21:00:00Z",
        tier="fable-weekly",
        tier_used_pct=100.0,
        tier_reset_at="2026-11-24T21:00:00Z",
        now=now,
    )
    router.usage_set(
        "agy",
        session_used_pct=10.0,
        session_reset_at="2026-11-22T09:41:00Z",
        weekly_used_pct=26.0,
        weekly_reset_at="2026-11-28T09:00:00Z",
        tier="3p-5h",
        tier_used_pct=0.0,
        tier_reset_at="2026-11-22T10:42:00Z",
        now=now,
    )
    router.usage_set(
        "agy",
        tier="3p-weekly",
        tier_used_pct=6.25,
        tier_reset_at="2026-11-27T02:00:00Z",
        now=now,
    )
    # Add an active $4 in-flight reservation on agy/gemini
    router.plan(
        "agy",
        {
            "format": "howhard-decision-v9",
            "decision_id": "b" * 64,
            "chosen": {"model": "agy/gemini-4-carbon-high", "effort": "high"},
            "cells": [{"model": "agy/gemini-4-carbon-high", "effort": "high", "needed_credit_usd": 4.0, "session_cost_usd": 0.4}],
        },
        prompt="Active $4 reservation on agy/gemini",
        now="2026-11-22T06:01:00Z",
    )

    app = build_app(lambda: router.snapshot(all_repos=False, now="2026-11-22T06:02:00Z"), refresh_interval=60.0)
    async with app.run_test(size=(130, 32)) as pilot:
        await pilot.pause()
        status = str(app.query_one("#route-status", Static).render())
        assert "codex BLOCKED · claude BLOCKED · agy OK" in status

        await pilot.press("u")
        await pilot.pause()
        subject = str(app.query_one("#route-subject", Static).render())
        assert "routes (u) · [QUOTAS]" in subject and "host: agy" in subject

        from textual.widgets import DataTable
        quotas = app.query_one("#quotas", DataTable)
        assert quotas.display is True

        codex_5h = quotas.get_row("codex:shared:5h")
        claude_shared_7d = quotas.get_row("claude:shared:7d")
        claude_fable_7d = quotas.get_row("claude:fable:7d")
        agy_gem_5h = quotas.get_row("agy:gemini:5h")
        agy_3p_5h = quotas.get_row("agy:3p:5h")

        assert codex_5h[0].plain == "codex" and codex_5h[1].plain == "plus-20" and codex_5h[2].plain == "BLOCKED"
        assert codex_5h[2].style == "bold red"
        assert claude_shared_7d[0].plain == "claude/shared" and claude_shared_7d[1].plain == "max-20x" and claude_shared_7d[2].plain == "BLOCKED"
        assert claude_fable_7d[0].plain == "claude/fable" and claude_fable_7d[2].plain == "BLOCKED"
        # Under host: agy, agy/gemini* has $35+$4 in-flight on 5h and is OK, while agy/3p* ($25 5h < $35 host rsv) is BLOCKED
        assert agy_gem_5h[0].plain == "agy/gemini*" and agy_gem_5h[2].plain == "OK" and agy_gem_5h[9].plain == "$35+$4"
        assert agy_gem_5h[2].style == "bold green"
        assert agy_3p_5h[0].plain == "agy/3p*" and agy_3p_5h[2].plain == "BLOCKED" and agy_3p_5h[9].plain == "$35"

        # Cycling host with 'H' moves host perspective off agy -> agy/3p flips to OK with $15 left on 5h!
        await pilot.press("H")
        await pilot.pause()
        agy_3p_target = quotas.get_row("agy:3p:5h")
        assert agy_3p_target[0].plain == "agy/3p" and agy_3p_target[2].plain == "OK" and agy_3p_target[10].plain == "$15"
        assert agy_3p_target[2].style == "bold green"


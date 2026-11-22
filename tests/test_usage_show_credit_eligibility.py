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
        subject = str(app.query_one("#route-subject", Static).render())
        assert "host: agy" in subject

        ledger = str(app.query_one("#quota-strip", Static).render())
        lines = {line.split()[0]: line for line in ledger.splitlines() if line.strip() and not line.lstrip().startswith(("──", "POOL", "5h", "7d"))}
        assert "BLOCKED" in lines["codex"] and "plus-20" in lines["codex"]
        assert "BLOCKED" in lines["claude/shared"] and "max-20x" in lines["claude/shared"]
        assert "BLOCKED" in lines["claude/fable"]
        # Under host: agy, agy/gemini* has $35+$4 in-flight on 5h and is OK, while agy/3p* ($25 5h < $35 host rsv) is BLOCKED
        assert "OK" in lines["agy/gemini*"] and "$35+$4" in lines["agy/gemini*"]
        assert "BLOCKED" in lines["agy/3p*"] and "$35" in lines["agy/3p*"]

        # Cycling host with 'H' moves host perspective off agy -> agy/3p flips to OK with $15 left on 5h!
        await pilot.press("H")
        await pilot.pause()
        ledger_target = str(app.query_one("#quota-strip", Static).render())
        target_lines = {line.split()[0]: line for line in ledger_target.splitlines() if line.strip() and not line.lstrip().startswith(("──", "POOL", "5h", "7d"))}
        assert "OK" in target_lines["agy/3p"] and "$15" in target_lines["agy/3p"]


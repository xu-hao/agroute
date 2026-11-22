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
    # First inspect agy/gemini 7d target and host reserves before in-flight reservation:
    pre_app = build_app(lambda: router.snapshot(all_repos=False, now="2026-11-22T06:00:30Z"), refresh_interval=60.0)
    async with pre_app.run_test(size=(160, 34)) as pre_pilot:
        await pre_pilot.pause()
        await pre_pilot.press("u")
        await pre_pilot.pause()
        from textual.widgets import DataTable
        pre_quotas = pre_app.query_one("#quotas", DataTable)
        assert [str(col.label) for col in pre_quotas.ordered_columns] == [
            "POOL", "PLAN", "ROLE", "STATUS", "WIN",
            "%USED", "%RSV(nom)", "%RSV(eff)", "%LEFT",
            "$CAP", "$USED", "$RSV(nom)", "$RSV(eff)", "$LEFT", "RESETS",
        ]
        # agy/gemini 7d ($1,350) without live rsv:
        # target: %RSV(nom)=5%, $RSV(nom)=$10, %RSV(eff)=5%, $RSV(eff)=$67.50
        # host:   %RSV(nom)=10%, $RSV(nom)=$35, %RSV(eff)=10%, $RSV(eff)=$135
        gem_7d_t = [c.plain for c in pre_quotas.get_row("agy:gemini:7d:target")]
        gem_7d_h = [c.plain for c in pre_quotas.get_row("agy:gemini:7d:host")]
        assert gem_7d_t[:14] == [
            "agy/gemini", "ultra-200", "target", "OK", "7d",
            "26%", "5%", "5%", "69%",
            "$1,350", "$351", "$10", "$67.50", "$932",
        ]
        assert gem_7d_h[:14] == [
            "agy/gemini", "ultra-200", "host", "OK", "7d",
            "26%", "10%", "10%", "64%",
            "$1,350", "$351", "$35", "$135", "$864",
        ]

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
    async with app.run_test(size=(160, 36)) as pilot:
        await pilot.pause()
        status = str(app.query_one("#route-status", Static).render())
        assert "codex BLOCKED · claude BLOCKED · agy OK" in status

        await pilot.press("u")
        await pilot.pause()
        subject = str(app.query_one("#route-subject", Static).render())
        assert "routes (u) · [QUOTAS]" in subject and "role filter: all (H)" in subject

        quotas = app.query_one("#quotas", DataTable)
        assert quotas.display is True

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
        all_keys = [str(r.key.value) for r in quotas.ordered_rows]
        assert all_keys == expected_all_keys
        assert [
            (
                quotas.get_row(k)[0].plain,
                quotas.get_row(k)[2].plain,
                quotas.get_row(k)[4].plain,
            )
            for k in all_keys
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

        # Exact concrete codex plus-20 examples ($20 5h, $50 7d, min_rem=$10, host_add=$25, max_pct=95%, host_rsv_pct=5%):
        codex_5h_t = quotas.get_row("codex:shared:5h:target")
        codex_5h_h = quotas.get_row("codex:shared:5h:host")
        codex_7d_t = quotas.get_row("codex:shared:7d:target")
        assert [c.plain for c in codex_5h_t[:14]] == [
            "codex", "plus-20", "target", "BLOCKED", "5h",
            "0%", "5%", "50%", "50%",
            "$20", "$0", "$10", "$10", "$10",
        ]
        assert codex_5h_t[3].style == "bold red"
        assert [c.plain for c in codex_5h_h[:14]] == [
            "codex", "plus-20", "host", "BLOCKED", "5h",
            "0%", "10%", "100%", "0%",
            "$20", "$0", "$35", "$35", "$0",
        ]
        assert [c.plain for c in codex_7d_t[:14]] == [
            "codex", "plus-20", "target", "BLOCKED", "7d",
            "81%", "5%", "20%", "0%",
            "$50", "$40.50", "$10", "$10", "$0",
        ]

        claude_shared_7d = quotas.get_row("claude:shared:7d:target")
        claude_fable_7d = quotas.get_row("claude:fable:7d:target")
        assert claude_shared_7d[0].plain == "claude/shared" and claude_shared_7d[1].plain == "max-20x" and claude_shared_7d[3].plain == "BLOCKED"
        assert claude_fable_7d[0].plain == "claude/fable" and claude_fable_7d[3].plain == "BLOCKED"

        # agy/gemini 5h host with $4 active in-flight reservation:
        agy_gem_5h_h = quotas.get_row("agy:gemini:5h:host")
        assert [c.plain for c in agy_gem_5h_h[:14]] == [
            "agy/gemini", "ultra-200", "host", "OK", "5h",
            "10%", "10%", "12.2%", "77.8%",
            "$320", "$32", "$35+$4", "$39", "$249",
        ]
        assert agy_gem_5h_h[3].style == "bold green"

        # agy/3p shows BOTH target OK ($15 left) and host BLOCKED ($35 rsv > $25 cap) simultaneously in "all":
        agy_3p_5h_t = quotas.get_row("agy:3p:5h:target")
        agy_3p_5h_h = quotas.get_row("agy:3p:5h:host")
        assert [c.plain for c in agy_3p_5h_t[:14]] == [
            "agy/3p", "ultra-200", "target", "OK", "5h",
            "0%", "5%", "40%", "60%",
            "$25", "$0", "$10", "$10", "$15",
        ]
        assert agy_3p_5h_t[3].style == "bold green"
        assert [c.plain for c in agy_3p_5h_h[:14]] == [
            "agy/3p", "ultra-200", "host", "BLOCKED", "5h",
            "0%", "10%", "100%", "0%",
            "$25", "$0", "$35", "$35", "$0",
        ]
        assert agy_3p_5h_h[3].style == "bold red"

        # Verify #route-detail on agy:gemini:5h:host spells out the additive host reserve breakdown + live reservation
        quotas.move_cursor(row=all_keys.index("agy:gemini:5h:host"))
        await pilot.pause()
        detail_host = str(app.query_one("#route-detail", Static).render())
        assert "agy/gemini [gemini · host]" in detail_host
        assert "nominal 10% (5%+5% host) / $35.00 ($10.00+$25.00 host) +$4.00 live -> effective 12.2% / $39.00" in detail_host

        # Cycling role filter with 'H': all -> target -> host -> agy -> claude -> codex -> all
        await pilot.press("H")  # target
        await pilot.pause()
        assert "role filter: target (H)" in str(app.query_one("#route-subject", Static).render())
        assert [str(r.key.value) for r in quotas.ordered_rows] == [
            "codex:shared:5h:target",
            "codex:shared:7d:target",
            "claude:shared:5h:target",
            "claude:shared:7d:target",
            "claude:fable:7d:target",
            "agy:gemini:5h:target",
            "agy:gemini:7d:target",
            "agy:3p:5h:target",
            "agy:3p:7d:target",
        ]

        await pilot.press("H")  # host
        await pilot.pause()
        assert "role filter: host (H)" in str(app.query_one("#route-subject", Static).render())
        assert [str(r.key.value) for r in quotas.ordered_rows] == [
            "codex:shared:5h:host",
            "codex:shared:7d:host",
            "claude:shared:5h:host",
            "claude:shared:7d:host",
            "claude:fable:7d:host",
            "agy:gemini:5h:host",
            "agy:gemini:7d:host",
            "agy:3p:5h:host",
            "agy:3p:7d:host",
        ]

        await pilot.press("H")  # agy (host for agy, target for codex/claude)
        await pilot.pause()
        assert "role filter: agy (H)" in str(app.query_one("#route-subject", Static).render())
        assert [str(r.key.value) for r in quotas.ordered_rows] == [
            "codex:shared:5h:target",
            "codex:shared:7d:target",
            "claude:shared:5h:target",
            "claude:shared:7d:target",
            "claude:fable:7d:target",
            "agy:gemini:5h:host",
            "agy:gemini:7d:host",
            "agy:3p:5h:host",
            "agy:3p:7d:host",
        ]

        await pilot.press("H")  # claude (host for claude, target for codex/agy)
        await pilot.pause()
        assert "role filter: claude (H)" in str(app.query_one("#route-subject", Static).render())
        assert [str(r.key.value) for r in quotas.ordered_rows] == [
            "codex:shared:5h:target",
            "codex:shared:7d:target",
            "claude:shared:5h:host",
            "claude:shared:7d:host",
            "claude:fable:7d:host",
            "agy:gemini:5h:target",
            "agy:gemini:7d:target",
            "agy:3p:5h:target",
            "agy:3p:7d:target",
        ]

        await pilot.press("H")  # codex (host for codex, target for claude/agy)
        await pilot.pause()
        assert "role filter: codex (H)" in str(app.query_one("#route-subject", Static).render())
        assert [str(r.key.value) for r in quotas.ordered_rows] == [
            "codex:shared:5h:host",
            "codex:shared:7d:host",
            "claude:shared:5h:target",
            "claude:shared:7d:target",
            "claude:fable:7d:target",
            "agy:gemini:5h:target",
            "agy:gemini:7d:target",
            "agy:3p:5h:target",
            "agy:3p:7d:target",
        ]

        await pilot.press("H")  # back to all
        await pilot.pause()
        assert "role filter: all (H)" in str(app.query_one("#route-subject", Static).render())
        assert [str(r.key.value) for r in quotas.ordered_rows] == expected_all_keys


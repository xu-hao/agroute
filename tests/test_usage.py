"""Tests for native quota and rate-limit ingestion, setting, probing, and window expiry (#3)."""

import json
import pytest

from agroute import AgRouteError, Router


def test_ingest_codex_jsonl_rate_limits_and_expiry(tmp_path):
    router = Router(project=tmp_path)
    codex_jsonl = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "th_1"}),
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "rate_limits": {
                                "primary": {
                                    "used_percent": 91.5,
                                    "window_minutes": 300,
                                    "resets_at": 1794163200,  # 2026-11-08T18:40:00Z
                                },
                                "secondary": {
                                    "used_percent": 40.0,
                                    "window_minutes": 10080,
                                    "resets_at": "2026-11-15T00:00:00Z",
                                },
                            }
                        },
                    },
                }
            ),
        ]
    )
    res = router.usage_ingest("codex", codex_jsonl, now="2026-11-08T17:00:00Z")
    assert res["usage"]["session_5h"]["used_pct"] == 91.5
    assert res["usage"]["session_5h"]["reset_at"] == "2026-11-08T18:40:00Z"
    assert res["usage"]["weekly_7d"]["used_pct"] == 40.0

    # Before reset_at: codex is ineligible (91.5% >= 90%)
    before = router.usage_show("codex", now="2026-11-08T18:00:00Z")["agents"]["codex"]
    assert before["eligible_as_target"] is False
    assert "session_5h" in before["target_exclusion_reason"]

    # At/after reset_at: session_5h expires automatically, weekly_7d remains 40% -> eligible!
    after = router.usage_show("codex", now="2026-11-08T18:40:00Z")["agents"]["codex"]
    assert after["session_5h"]["used_pct"] is None
    assert after["weekly_7d"]["used_pct"] == 40.0
    assert after["eligible_as_target"] is True


def test_ingest_claude_usage_text_and_rate_limit_event(tmp_path):
    router = Router(project=tmp_path)
    claude_text = """
Current session: 45% used (resets 2026-11-08T21:00:00Z)
Current week (all models): 72% used (resets 2026-11-14T00:00:00Z)
Current week (Opus): 96% used (resets 2026-11-14T00:00:00Z)
Current week (Sonnet): 50% used (resets 2026-11-14T00:00:00Z)
"""
    res = router.usage_ingest("claude", claude_text, now="2026-11-08T17:00:00Z")
    assert res["usage"]["session_5h"]["used_pct"] == 45.0
    assert res["usage"]["weekly_7d"]["used_pct"] == 72.0
    assert res["usage"]["tiers"]["opus"]["used_pct"] == 96.0
    assert res["usage"]["tiers"]["sonnet"]["used_pct"] == 50.0

    # Ingest Claude AI usage limit reached|<epoch>
    router.usage_ingest("claude", "Claude AI usage limit reached|1794166800", now="2026-11-08T17:00:00Z")
    blocked = router.usage_show("claude", now="2026-11-08T18:00:00Z")["agents"]["claude"]
    assert blocked["rate_limited"] is True
    assert blocked["eligible_as_target"] is False

    # After cooldown expires:
    unblocked = router.usage_show("claude", now="2026-11-08T19:45:00Z")["agents"]["claude"]
    assert unblocked["rate_limited"] is False
    assert unblocked["eligible_as_target"] is True


def test_ingest_agy_tier_and_remaining_percentage(tmp_path):
    router = Router(project=tmp_path)
    agy_text = """
5h limit: 20% remaining (resets at 2026-11-08T20:00:00Z)
pro: 97% used (resets at 2026-11-08T20:00:00Z)
flash: 15% used (resets at 2026-11-08T20:00:00Z)
"""
    res = router.usage_ingest("agy", agy_text, now="2026-11-08T17:00:00Z")
    assert res["usage"]["session_5h"]["used_pct"] == pytest.approx(80.0)
    assert res["usage"]["tiers"]["pro"]["used_pct"] == 97.0
    assert res["usage"]["tiers"]["flash"]["used_pct"] == 15.0


def test_usage_probe_and_unrecognized_input_refusal(tmp_path):
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        """
[agents.codex]
usage_probe_cmd = ["python3", "-c", "print('5h limit: 33% used (resets 2026-11-08T22:00:00Z)')"]
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=tmp_path)
    probed = router.usage_probe("codex", now="2026-11-08T17:00:00Z")
    assert probed["probed"]["codex"]["session_5h"]["used_pct"] == 33.0

    with pytest.raises(AgRouteError, match="no recognizable native usage"):
        router.usage_ingest("codex", "completely unrelated output line")

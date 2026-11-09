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

    # Ingest Claude Code 2.1+ rate_limit_event with unifiedWindows and camelCase resetsAt
    router.usage_ingest(
        "claude",
        json.dumps(
            {
                "type": "rate_limit_event",
                "rate_limit_info": {
                    "status": "allowed",
                    "resetsAt": 1794194400,
                    "rateLimitType": "five_hour",
                    "unifiedWindows": {
                        "five_hour": {"utilization": 0, "resetsAt": 1794194400, "status": "allowed"},
                        "seven_day": {"utilization": 0.25, "resetsAt": 1794344400, "status": "allowed"},
                    },
                },
            }
        ),
        now="2026-11-08T17:00:00Z",
    )
    unified_snap = router.usage_show("claude", now="2026-11-08T18:00:00Z")["agents"]["claude"]
    assert unified_snap["session_5h"]["used_pct"] == 0.0
    assert unified_snap["weekly_7d"]["used_pct"] == 25.0

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


def test_auto_discovery_from_codex_rollout_sessions(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    day_dir = codex_home / "sessions" / "2026" / "11" / "08"
    day_dir.mkdir(parents=True)
    (day_dir / "rollout-1.jsonl").write_text(
        json.dumps(
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "rate_limits": {
                        "primary": {
                            "used_percent": 12.5,
                            "window_minutes": 10080,
                            "resets_at": 1794775586,
                        }
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    router = Router(project=tmp_path)
    snap = router.usage_show("codex", now="2026-11-08T22:00:00Z")["agents"]["codex"]
    assert snap["weekly_7d"]["used_pct"] == 12.5
    assert snap["source"] == "discovered"
    assert snap["models"]["gpt-5.4"]["weekly_7d"]["used_pct"] == 12.5
    assert snap["models"]["gpt-5.4"]["eligible"] is True


def test_per_model_quota_from_agy_claude_and_codex_structured_commands(tmp_path):
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        """
[agents.agy]
cli_candidates = [
  { model = "gemini-4-argon-high", effort = "high" },
  { model = "gemini-3.8-flash-low", effort = "low" },
  { model = "claude-opus-5-5-medium", effort = "medium" },
]
""",
        encoding="utf-8",
    )
    router = Router(cfg, project=tmp_path)

    # 1. Antigravity /usage JSON with separate Gemini, 3P (Claude/GPT), and Gemini 4 Argon buckets
    agy_usage_json = json.dumps(
        {
            "status": "SUCCESS",
            "command": {
                "name": "usage",
                "data": {
                    "groups": [
                        {
                            "name": "Gemini Models",
                            "buckets": [
                                {
                                    "id": "gemini-weekly",
                                    "window": "weekly",
                                    "remaining_fraction": 0.976,
                                    "reset_time": "2026-11-15T18:07:02Z",
                                },
                                {
                                    "id": "gemini-5h",
                                    "window": "5h",
                                    "remaining_fraction": 0.938,
                                    "reset_time": "2026-11-08T23:07:02Z",
                                },
                            ],
                        },
                        {
                            "name": "Claude and GPT models",
                            "buckets": [
                                {
                                    "id": "3p-weekly",
                                    "window": "weekly",
                                    "remaining_fraction": 1.0,
                                    "reset_time": "2026-11-15T22:44:42Z",
                                },
                                {
                                    "id": "3p-5h",
                                    "window": "5h",
                                    "remaining_fraction": 1.0,
                                    "reset_time": "2026-11-09T03:44:42Z",
                                },
                            ],
                        },
                        {
                            "name": "Gemini 4 Argon",
                            "buckets": [
                                {
                                    "id": "gemini-argon-wtu-weekly",
                                    "window": "weekly",
                                    "remaining_fraction": 0.03,
                                    "reset_time": "2026-11-15T16:30:26Z",
                                }
                            ],
                        },
                    ]
                },
            },
        }
    )
    router.usage_ingest("agy", agy_usage_json, now="2026-11-08T22:00:00Z")
    agy_snap = router.usage_show("agy", now="2026-11-08T22:05:00Z")["agents"]["agy"]
    assert agy_snap["models"]["gemini-4-argon-high"]["weekly_7d"]["used_pct"] == pytest.approx(97.0)
    assert agy_snap["models"]["gemini-4-argon-high"]["session_5h"]["used_pct"] == pytest.approx(6.2)
    assert agy_snap["models"]["gemini-4-argon-high"]["eligible"] is False
    assert agy_snap["models"]["gemini-3.8-flash-low"]["weekly_7d"]["used_pct"] == pytest.approx(2.4)
    assert agy_snap["models"]["gemini-3.8-flash-low"]["eligible"] is True
    assert agy_snap["models"]["claude-opus-5-5-medium"]["weekly_7d"]["used_pct"] == pytest.approx(0.0)
    assert agy_snap["models"]["claude-opus-5-5-medium"]["session_5h"]["used_pct"] == pytest.approx(0.0)
    assert agy_snap["models"]["claude-opus-5-5-medium"]["eligible"] is True

    # Verify candidates filtering excludes only gemini-4-argon-high while keeping flash and claude-opus-5-5-medium
    cands = router.candidates("codex", now="2026-11-08T22:05:00Z")
    admitted_models = [c["model"] for c in cands["candidates"]]
    assert "agy/gemini-4-argon-high" not in admitted_models
    assert "agy/gemini-3.8-flash-low" in admitted_models
    assert "agy/claude-opus-5-5-medium" in admitted_models

    # 2. Claude /usage structured usage_report
    claude_usage_json = json.dumps(
        {
            "type": "assistant",
            "usage_report": {
                "rate_limits": {
                    "limits": [
                        {
                            "kind": "session",
                            "group": "session",
                            "percent": 2,
                            "resets_at": "2026-11-09T03:20:00Z",
                            "scope": None,
                        },
                        {
                            "kind": "weekly_all",
                            "group": "weekly",
                            "percent": 14,
                            "resets_at": "2026-11-10T21:00:00Z",
                            "scope": None,
                        },
                        {
                            "kind": "weekly_opus",
                            "group": "weekly",
                            "percent": 96,
                            "resets_at": "2026-11-10T21:00:00Z",
                            "scope": "opus",
                        },
                    ]
                }
            },
        }
    )
    router.usage_ingest("claude", claude_usage_json, now="2026-11-08T22:00:00Z")
    claude_snap = router.usage_show("claude", now="2026-11-08T22:05:00Z")["agents"]["claude"]
    assert claude_snap["models"]["claude-opus-4-6"]["weekly_7d"]["used_pct"] == 96.0
    assert claude_snap["models"]["claude-opus-4-6"]["eligible"] is False
    assert claude_snap["models"]["claude-sonnet-4-6"]["weekly_7d"]["used_pct"] == 14.0
    assert claude_snap["models"]["claude-sonnet-4-6"]["eligible"] is True

    # 3. Codex app-server account/rateLimits/read JSON-RPC response
    codex_rpc_json = json.dumps(
        {
            "id": 2,
            "result": {
                "ordinaryUsageAllowed": True,
                "rateLimits": {
                    "limitId": "codex",
                    "primary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 1794775586},
                    "secondary": None,
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitId": "codex",
                        "primary": {"usedPercent": 7, "windowDurationMins": 10080, "resetsAt": 1794775586},
                    }
                },
            },
        }
    )
    router.usage_ingest("codex", codex_rpc_json, now="2026-11-08T22:00:00Z")
    codex_snap = router.usage_show("codex", now="2026-11-08T22:05:00Z")["agents"]["codex"]
    assert codex_snap["models"]["gpt-5.4"]["weekly_7d"]["used_pct"] == 7.0
    assert codex_snap["models"]["gpt-5.4"]["eligible"] is True

"""Claude quota evidence and probe freshness at public boundaries (issue #21)."""

import json
import os
from pathlib import Path
import sys

import pytest

from agroute import AgRouteError, Router
from agroute.cli import main

RESET = "2026-11-10T21:00:00Z"
T0 = "2026-11-09T12:00:00Z"
T1 = "2026-11-09T12:10:00Z"
T2 = "2026-11-09T12:20:00Z"


def setup(tmp_path, report=None):
    script = tmp_path / "probe.py"
    script.write_text("print(" + repr(json.dumps(report or {})) + ")\n")
    config = tmp_path / "agroute.toml"
    config.write_text(
        "[agents.claude]\nusage_probe_cmd="
        + json.dumps([sys.executable, str(script)])
        + """\nnative_candidates=[
{model="claude-fable-5-2",effort="high",tier="fable"},
{model="claude-opus-5-5",effort="high",tier="opus"}]
"""
    )
    return Router(config, project=tmp_path), script


def usage(limits):
    return {"type": "assistant", "usage_report": {"rate_limits": {"limits": limits}}}


def limit(kind="weekly_all", percent=0, scope=None, **extra):
    return {
        "kind": kind,
        "percent": percent,
        "resets_at": RESET,
        "scope": scope,
        **extra,
    }


def event(status="rejected", scope=None, kind="seven_day", timestamp=T1):
    return {
        "type": "rate_limit_event",
        "timestamp": timestamp,
        "rate_limit_info": {
            "status": status,
            "resetsAt": RESET,
            "rateLimitType": kind,
            "scope": scope,
            "overageStatus": "rejected" if status == "rejected" else "allowed",
            "overageDisabledReason": "org_level_disabled",
            "isUsingOverage": False,
        },
    }


def transcript(value):
    projects = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "owned-fixture"
    projects.mkdir(parents=True, exist_ok=True)
    path = projects / "run.jsonl"
    path.write_text(json.dumps(value) + "\n")
    return path


def test_structured_scope_and_one_bad_entry_do_not_abort_cli_probe(tmp_path, capsys):
    report = usage(
        [
            limit(
                "weekly_fable",
                96,
                {"model": {"display_name": "Fable"}, "surface": None},
            ),
            {"kind": "weekly", "scope": {"unexpected": []}, "percent": 42},
            limit("weekly_all", 14),
        ]
    )
    router, script = setup(tmp_path, report)
    assert (
        main(
            [
                "usage",
                "probe",
                "--agent",
                "claude",
                "--config",
                str(tmp_path / "agroute.toml"),
                "--now",
                T0,
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["probed"]["claude"]["tiers"]["fable-weekly"]["used_pct"] == 96
    assert result["probed"]["claude"]["weekly_7d"]["used_pct"] == 14
    assert result["warnings"] and "scope" in " ".join(result["warnings"])
    candidates = router.candidates("claude", now=T0)
    assert "claude/claude-fable-5-2" not in {
        c["model"] for c in candidates["candidates"]
    }
    assert any(c["model"] == "claude/claude-opus-5-5" for c in candidates["candidates"])


def test_surface_scope_is_sanitized_but_matches_only_its_model(tmp_path):
    router, _ = setup(
        tmp_path,
        usage(
            [
                limit(
                    "weekly_fable",
                    99,
                    {"model": {"display_name": "Fable"}, "surface": "Claude Code"},
                )
            ]
        ),
    )
    result = router.usage_probe("claude", now=T0)
    assert "fable-claude-code-weekly" in result["probed"]["claude"]["tiers"]
    result = router.candidates("claude", now=T0)
    assert not any(
        c["model"] == "claude/claude-fable-5-2" for c in result["candidates"]
    )
    assert any(c["model"] == "claude/claude-opus-5-5" for c in result["candidates"])


def test_new_rejected_transcript_overrides_an_old_zero_snapshot_and_expires(tmp_path):
    router, _ = setup(tmp_path, usage([limit()]))
    router.usage_probe("claude", now=T0)
    transcript(event())
    snapshot = router.usage_show("claude", now=T2)["agents"]["claude"]
    assert snapshot["rate_limited"] and snapshot["cooldown_until"] == RESET
    assert snapshot["weekly_7d"]["used_pct"] is None
    result = router.candidates("claude", now=T2)
    excluded = [c for c in result["excluded"] if c["agent"] == "claude"]
    assert excluded and "weekly limit rejected until " + RESET in excluded[0]["reason"]
    assert not any(c["model"].startswith("claude/") for c in result["candidates"])
    restored = router.candidates("claude", now="2026-11-10T21:00:01Z")
    assert sum(c["model"].startswith("claude/") for c in restored["candidates"]) == 2


def test_scoped_rejection_blocks_only_fable(tmp_path):
    router, _ = setup(tmp_path)
    transcript(event(scope={"model": {"display_name": "fable"}, "surface": None}))
    result = router.candidates("claude", now=T2)
    assert not any(
        c["model"] == "claude/claude-fable-5-2" for c in result["candidates"]
    )
    assert any(c["model"] == "claude/claude-opus-5-5" for c in result["candidates"])
    assert any(
        "fable" in c["reason"] and "rejected" in c["reason"] for c in result["excluded"]
    )


def test_plan_upgrade_current_report_restores_access_before_old_reset(tmp_path):
    router, script = setup(tmp_path, usage([limit(percent=100)]))
    router.usage_probe("claude", now=T0)
    path = transcript(event())
    assert router.usage_show("claude", now=T1)["agents"]["claude"]["rate_limited"]
    script.write_text("print(" + repr(json.dumps(usage([limit(percent=0)]))) + ")\n")
    router.usage_probe("claude", now=T2)
    # A later write to the same transcript does not make its old rejection newer.
    path.write_text(
        path.read_text()
        + json.dumps({"type": "assistant", "message": "unrelated fixture"})
        + "\n"
    )
    restored = router.usage_show("claude", now="2026-11-09T12:21:00Z")["agents"][
        "claude"
    ]
    assert not restored["rate_limited"] and restored["cooldown_until"] is None
    assert restored["weekly_7d"]["used_pct"] == 0
    assert restored["eligible_as_target"]
    assert (
        sum(
            c["model"].startswith("claude/")
            for c in router.candidates("claude", now=T2)["candidates"]
        )
        == 2
    )


def test_accepted_restoration_without_measurements_clears_obsolete_counter(tmp_path):
    router, _ = setup(tmp_path)
    router.usage_ingest("claude", json.dumps(usage([limit(percent=100)])), now=T0)
    router.usage_ingest("claude", json.dumps(event()), now=T1)
    router.usage_ingest("claude", json.dumps(event("allowed", timestamp=T2)), now=T2)
    state = router.usage_show("claude", now=T2)["agents"]["claude"]
    assert not state["rate_limited"] and state["weekly_7d"]["used_pct"] is None
    assert state["eligible_as_target"]


def test_partial_accepted_report_does_not_clear_another_window_or_model(tmp_path):
    router, script = setup(tmp_path)
    router.usage_ingest(
        "claude", json.dumps(event(scope={"model": {"display_name": "fable"}})), now=T1
    )
    script.write_text(
        "print("
        + repr(
            json.dumps(usage([limit("session", 1), limit("weekly_opus", 2, "opus")]))
        )
        + ")\n"
    )
    router.usage_probe("claude", now=T2)
    result = router.candidates("claude", now=T2)
    assert not any(
        c["model"] == "claude/claude-fable-5-2" for c in result["candidates"]
    )
    assert any(c["model"] == "claude/claude-opus-5-5" for c in result["candidates"])


def test_failed_probe_preserves_data_time_and_surfaces_unknown_stale_state(tmp_path):
    router, script = setup(tmp_path, usage([limit()]))
    router.usage_probe("claude", now=T0)
    script.write_text("print('unrecognized output')\nraise SystemExit(1)\n")
    failed = "2026-11-09T12:45:00Z"
    with pytest.raises(AgRouteError):
        router.usage_probe("claude", now=failed)
    result = router.usage_show("claude", now=failed)
    state = result["agents"]["claude"]
    assert state["updated_at"] == T0
    assert (
        state["probe"]["succeeded_at"] == T0 and state["probe"]["failed_at"] == failed
    )
    assert state["weekly_7d"]["used_pct"] is None
    assert any("stale" in w and "failed" in w for w in result["warnings"])
    assert any(
        "failed" in w for w in router.candidates("claude", now=failed)["warnings"]
    )


def test_staleness_bound_is_configurable(tmp_path):
    router, _ = setup(tmp_path, usage([limit()]))
    config = tmp_path / "agroute.toml"
    config.write_text(config.read_text() + "usage_stale_after_seconds=60\n")
    router = Router(config, project=tmp_path)
    router.usage_probe("claude", now=T0)
    assert not router.usage_show("claude", now="2026-11-09T12:00:30Z")["warnings"]
    assert any(
        "stale" in w
        for w in router.usage_show("claude", now="2026-11-09T12:02:00Z")["warnings"]
    )


def test_all_claude_rejected_cli_keeps_json_exclusions(tmp_path, capsys):
    router, _ = setup(tmp_path)
    transcript(event())
    assert (
        main(
            [
                "candidates",
                "--host",
                "claude",
                "--include-agents",
                "claude",
                "--config",
                str(tmp_path / "agroute.toml"),
                "--now",
                T2,
                "--json",
            ]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["candidates"] == []
    assert "weekly limit rejected until " + RESET in result["excluded"][0]["reason"]


def test_untimestamped_old_rejection_does_not_replay_but_new_occurrence_does(tmp_path):
    router, _ = setup(tmp_path, usage([limit()]))
    rejected = event()
    rejected.pop("timestamp")
    path = transcript(rejected)
    router.usage_probe("claude", now=T2)
    assert router.usage_show("claude", now=T2)["agents"]["claude"]["eligible_as_target"]
    path.write_text(path.read_text() + json.dumps({"type": "assistant"}) + "\n")
    assert router.usage_show("claude", now=T2)["agents"]["claude"]["eligible_as_target"]
    path.write_text(path.read_text() + json.dumps(rejected) + "\n")
    assert not router.usage_show("claude", now=T2)["agents"]["claude"][
        "eligible_as_target"
    ]


def test_successful_exit_without_quota_evidence_does_not_clear_rejection(tmp_path):
    router, script = setup(tmp_path)
    router.usage_ingest("claude", json.dumps(event()), now=T1)
    script.write_text('print(\'{"type":"result","is_error":false,"result":"OK"}\')\n')
    with pytest.raises(AgRouteError, match="no recognizable"):
        router.usage_probe("claude", now=T2)
    assert not router.usage_show("claude", now=T2)["agents"]["claude"][
        "eligible_as_target"
    ]


def test_recovery_preserves_current_measured_counter_and_policy(tmp_path):
    router, script = setup(tmp_path)
    router.usage_ingest("claude", json.dumps(event()), now=T1)
    script.write_text("print(" + repr(json.dumps(usage([limit(percent=99)]))) + ")\n")
    router.usage_probe("claude", now=T2)
    state = router.usage_show("claude", now=T2)["agents"]["claude"]
    assert not state["rate_limited"] and state["weekly_7d"]["used_pct"] == 99
    assert (
        not state["eligible_as_target"]
        and "effective limit" in state["target_exclusion_reason"]
    )


def test_failed_probe_does_not_erase_fresh_measured_usage(tmp_path):
    router, script = setup(tmp_path, usage([limit(percent=99)]))
    router.usage_probe("claude", now=T0)
    script.write_text("print('unrecognized')\n")
    with pytest.raises(AgRouteError):
        router.usage_probe("claude", now=T1)
    state = router.usage_show("claude", now=T1)["agents"]["claude"]
    assert state["weekly_7d"]["used_pct"] == 99 and not state["eligible_as_target"]


def test_accepted_session_does_not_clear_rejected_weekly_same_model(tmp_path):
    router, _ = setup(tmp_path)
    scope = {"model": {"display_name": "fable"}}
    router.usage_ingest("claude", json.dumps(event(scope=scope)), now=T1)
    router.usage_ingest(
        "claude", json.dumps(event("allowed", scope, "five_hour", T2)), now=T2
    )
    result = router.candidates("claude", now=T2)
    assert not any(
        c["model"] == "claude/claude-fable-5-2" for c in result["candidates"]
    )
    assert any(c["model"] == "claude/claude-opus-5-5" for c in result["candidates"])


def test_older_ingested_evidence_does_not_refresh_data_time(tmp_path):
    router, _ = setup(tmp_path, usage([limit()]))
    router.usage_probe("claude", now=T2)
    router.usage_ingest(
        "claude", json.dumps(event(timestamp=T1)), now="2026-11-09T12:25:00Z"
    )
    state = router.usage_show("claude", now="2026-11-09T12:25:00Z")["agents"]["claude"]
    assert state["updated_at"] == T2 and not state["rate_limited"]


def test_scoped_credit_rejection_without_window_excludes_only_that_model(tmp_path):
    router, _ = setup(tmp_path)
    rejected = event(scope={"model": {"display_name": "fable"}}, kind=None)
    rejected["rate_limit_info"]["errorCode"] = "credits_required"
    router.usage_ingest("claude", json.dumps(rejected), now=T1)
    result = router.candidates("claude", now=T2)
    assert not any(
        c["model"] == "claude/claude-fable-5-2" for c in result["candidates"]
    )
    assert any(c["model"] == "claude/claude-opus-5-5" for c in result["candidates"])


def test_camel_case_rejected_event_is_discovered(tmp_path):
    router, _ = setup(tmp_path)
    rejected = event()
    rejected["rateLimitInfo"] = rejected.pop("rate_limit_info")
    transcript(rejected)
    assert router.usage_show("claude", now=T2)["agents"]["claude"]["rate_limited"]


def test_allowed_event_without_rejection_preserves_fresh_measured_counter(tmp_path):
    router, _ = setup(tmp_path)
    router.usage_ingest("claude", json.dumps(usage([limit(percent=99)])), now=T1)
    router.usage_ingest("claude", json.dumps(event("allowed", timestamp=T2)), now=T2)
    state = router.usage_show("claude", now=T2)["agents"]["claude"]
    assert state["weekly_7d"]["used_pct"] == 99 and not state["eligible_as_target"]


def test_terminal_weekly_429_can_be_superseded_by_current_weekly_report(tmp_path):
    router, _ = setup(tmp_path, usage([limit()]))
    router.usage_ingest(
        "claude",
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "result": "You've hit your weekly limit · resets 9pm (UTC)",
            }
        ),
        now=T1,
    )
    assert router.usage_show("claude", now=T1)["agents"]["claude"]["rate_limited"]
    router.usage_probe("claude", now=T2)
    assert router.usage_show("claude", now=T2)["agents"]["claude"]["eligible_as_target"]


@pytest.mark.parametrize("current_reset", [RESET, "2026-11-16T15:00:00Z"])
def test_legacy_probe_cooldown_and_counter_restore_after_plan_upgrade(
    tmp_path, current_reset
):
    router, _ = setup(tmp_path, usage([limit(resets_at=current_reset)]))
    router.usage_set(
        "claude",
        weekly_used_pct=100,
        weekly_reset_at=RESET,
        rate_limited=True,
        cooldown_until=RESET,
        now=T1,
    )
    path = Path(router.paths()["state_root"]) / "usage.json"
    stored = json.loads(path.read_text())
    legacy = stored["agents"]["claude"]
    legacy["source"] = "probe"
    legacy.pop("probe")
    legacy.pop("availability")
    path.write_text(json.dumps(stored))
    assert router.usage_show("claude", now=T1)["agents"]["claude"]["rate_limited"]
    router.usage_probe("claude", now=T2)
    state = router.usage_show("claude", now=T2)["agents"]["claude"]
    assert not state["rate_limited"] and state["cooldown_until"] is None
    assert state["weekly_7d"]["used_pct"] == 0 and state["eligible_as_target"]
    assert state["weekly_7d"]["reset_at"] == current_reset


def test_legacy_unknown_cooldown_is_not_cleared_by_partial_report(tmp_path):
    router, _ = setup(tmp_path, usage([limit("session", 1)]))
    router.usage_set("claude", rate_limited=True, cooldown_until=RESET, now=T1)
    path = Path(router.paths()["state_root"]) / "usage.json"
    stored = json.loads(path.read_text())
    legacy = stored["agents"]["claude"]
    legacy["source"] = "probe"
    legacy.pop("probe")
    legacy.pop("availability")
    path.write_text(json.dumps(stored))
    router.usage_probe("claude", now=T2)
    assert not router.usage_show("claude", now=T2)["agents"]["claude"][
        "eligible_as_target"
    ]
    router.usage_ingest(
        "claude", json.dumps(event("allowed", kind=None, timestamp=T2)), now=T2
    )
    assert router.usage_show("claude", now=T2)["agents"]["claude"]["eligible_as_target"]


def test_scoped_credit_rejection_survives_partial_quota_counter(tmp_path):
    scope = {"model": {"display_name": "fable"}}
    router, script = setup(tmp_path, usage([limit("weekly_fable", 0, scope)]))
    router.usage_ingest("claude", json.dumps(event(scope=scope, kind=None)), now=T1)
    router.usage_probe("claude", now=T2)
    result = router.candidates("claude", now=T2)
    assert not any(
        c["model"] == "claude/claude-fable-5-2" for c in result["candidates"]
    )
    assert any(c["model"] == "claude/claude-opus-5-5" for c in result["candidates"])
    restored_at = "2026-11-09T12:25:00Z"
    router.usage_ingest(
        "claude",
        json.dumps(event("allowed", scope, None, restored_at)),
        now=restored_at,
    )
    assert any(
        c["model"] == "claude/claude-fable-5-2"
        for c in router.candidates("claude", now=restored_at)["candidates"]
    )


def test_unscoped_credit_rejection_survives_partial_quota_counter(tmp_path):
    router, _ = setup(tmp_path, usage([limit(percent=0)]))
    router.usage_ingest("claude", json.dumps(event(kind=None)), now=T1)
    router.usage_probe("claude", now=T2)
    assert not router.usage_show("claude", now=T2)["agents"]["claude"][
        "eligible_as_target"
    ]
    restored_at = "2026-11-09T12:25:00Z"
    router.usage_ingest(
        "claude",
        json.dumps(event("allowed", kind=None, timestamp=restored_at)),
        now=restored_at,
    )
    assert router.usage_show("claude", now=restored_at)["agents"]["claude"][
        "eligible_as_target"
    ]

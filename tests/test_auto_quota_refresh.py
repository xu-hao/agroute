"""Routing refreshes quota evidence without calling real provider backends."""

import pytest

from agroute import AgRouteError, Router
from agroute.adapters import get_adapter

T0 = "2026-11-09T12:00:00Z"
T1 = "2026-11-09T12:10:00Z"
STALE = "2026-11-09T12:31:00Z"
RESET = "2026-11-09T14:00:00Z"


def patch_probe(monkeypatch, agent, patches):
    calls = []
    def probe(cwd):
        calls.append(cwd)
        value = patches[min(len(calls) - 1, len(patches) - 1)]
        if isinstance(value, Exception):
            raise value
        return value
    monkeypatch.setattr(get_adapter(agent), "probe_usage", probe)
    return calls


def session(used=10, reset=RESET):
    return {"session_5h": {"used_pct": used, "reset_at": reset}}


def decision():
    return {"decision_id": "test", "chosen": {"model": "codex/gpt-5.4", "effort": "high"}}


@pytest.mark.parametrize("boundary", ["candidates", "inventory", "plan"])
def test_missing_quota_is_refreshed_before_each_routing_boundary(tmp_path, monkeypatch, boundary):
    router = Router(project=tmp_path)
    calls = patch_probe(monkeypatch, "codex", [session()])
    result = getattr(router, boundary)("codex", **({"decision": decision()} if boundary == "plan" else {}), include_agents=["codex"], now=T0)
    assert len(calls) == 1
    if boundary == "candidates":
        assert any(c["model"] == "codex/gpt-5.4" for c in result["candidates"])
    elif boundary == "inventory":
        assert any(c["model"] == "codex/gpt-5.4" for c in result["inventories"]["router"])
    else:
        assert result["target_agent"] == "codex"
    state = router.usage_show("codex", now=T0)["agents"]["codex"]
    assert state["session_5h"]["used_pct"] == 10
    assert state["weekly_7d"]["used_pct"] is None


def test_fresh_exhausted_quota_is_not_refreshed_away(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    router.usage_set("codex", session_used_pct=99, session_reset_at=RESET, now=T0)
    calls = patch_probe(monkeypatch, "codex", [session(0)])
    with pytest.raises(AgRouteError) as exc:
        router.candidates("codex", include_agents=["codex"], now=T1)
    assert "session_5h" in str(exc.value)
    assert not calls


def test_stale_blocked_quota_refreshes_and_recovers(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    router.usage_set("codex", session_used_pct=99, session_reset_at=RESET, now=T0)
    calls = patch_probe(monkeypatch, "codex", [session(5)])
    result = router.candidates("codex", include_agents=["codex"], now=STALE)
    assert len(calls) == 1
    assert any(c["model"] == "codex/gpt-5.4" for c in result["candidates"])


def test_expired_quota_requires_current_evidence(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    router.usage_set("codex", session_used_pct=99, session_reset_at=T1, now=T0)
    calls = patch_probe(monkeypatch, "codex", [session(99)])
    with pytest.raises(AgRouteError) as exc:
        router.candidates("codex", include_agents=["codex"], now=T1)
    assert len(calls) == 1 and "session_5h" in str(exc.value)


def test_failed_refresh_excludes_only_failed_provider_then_recovers(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    router.usage_set("claude", session_used_pct=10, session_reset_at=RESET, now=STALE)
    calls = patch_probe(monkeypatch, "codex", [AgRouteError("probe unavailable")])
    claude = patch_probe(monkeypatch, "claude", [AgRouteError("unexpected probe")])
    result = router.candidates("codex", include_agents=["codex", "claude"], now=STALE)
    assert len(calls) == 2 and not claude
    assert any(c["model"].startswith("claude/") for c in result["candidates"])
    assert not any(c["model"].startswith("codex/") for c in result["candidates"])
    reason = next(e["reason"] for e in result["excluded"] if e["agent"] == "codex")
    assert "incident" in reason and "retry" in reason and "codex" in reason
    state = router.usage_show("codex", now=STALE)["agents"]["codex"]
    assert state["probe"]["failed_at"] == STALE and state["updated_at"] is None
    calls = patch_probe(monkeypatch, "codex", [session()])
    recovered = router.candidates("codex", include_agents=["codex"], now=STALE)
    assert len(calls) == 1 and recovered["candidates"]


def test_partial_report_cannot_refresh_another_stale_window(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    router.usage_set("codex", session_used_pct=10, session_reset_at=RESET,
                     weekly_used_pct=20, weekly_reset_at=RESET, now=T0)
    calls = patch_probe(monkeypatch, "codex", [session(5)])
    with pytest.raises(AgRouteError) as exc:
        router.candidates("codex", include_agents=["codex"], now=STALE)
    assert len(calls) == 2
    assert "retry" in str(exc.value)


def test_command_timeout_is_bounded_and_structured_then_recovers(tmp_path, monkeypatch):
    import subprocess

    config = tmp_path / "agroute.toml"
    config.write_text('[agents.codex]\nusage_probe_cmd = ["fixture-probe"]\n')
    router = Router(config, project=tmp_path)
    calls = []
    def timeout(cmd, **kwargs):
        calls.append((cmd, kwargs["timeout"]))
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AgRouteError) as exc:
        router.candidates("codex", include_agents=["codex"], now=T0)
    assert len(calls) == 2 and all(call[1] == 15 for call in calls)
    assert exc.value.details["candidates"] == []
    assert "retry" in exc.value.details["excluded"][0]["reason"]
    state = router.usage_show("codex", now=T0)["agents"]["codex"]
    assert state["probe"]["failed_at"] == T0 and state["updated_at"] is None

    def succeed(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, '5h limit: 10% used (resets 2026-11-09T14:00:00Z)', '')
    monkeypatch.setattr(subprocess, "run", succeed)
    assert router.candidates("codex", include_agents=["codex"], now=T0)["candidates"]


@pytest.mark.parametrize("agent", ["codex", "claude", "agy"])
def test_each_provider_refreshes_missing_evidence(tmp_path, monkeypatch, agent):
    router = Router(project=tmp_path)
    calls = patch_probe(monkeypatch, agent, [session()])
    assert router.candidates(agent, include_agents=[agent], now=T0)["candidates"]
    assert len(calls) == 1


def test_offline_usage_show_never_probes(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    calls = patch_probe(monkeypatch, "codex", [AgRouteError("unexpected probe")])
    router.usage_show("codex", now=T0)
    assert not calls


def test_automatic_refresh_retries_once_after_transient_failure(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    calls = patch_probe(monkeypatch, "codex", [AgRouteError("temporary"), session()])
    result = router.candidates("codex", include_agents=["codex"], now=T0)
    assert len(calls) == 2 and result["candidates"]


@pytest.mark.parametrize("agent", ["codex", "claude", "agy"])
def test_each_provider_uses_configured_freshness_bound(tmp_path, monkeypatch, agent):
    config = tmp_path / "agroute.toml"
    config.write_text(f'[agents.{agent}]\nusage_stale_after_seconds = 60\n')
    router = Router(config, project=tmp_path)
    router.usage_set(agent, session_used_pct=10, session_reset_at=RESET, now=T0)
    calls = patch_probe(monkeypatch, agent, [session(20)])
    router.candidates(agent, include_agents=[agent], now="2026-11-09T12:00:30Z")
    assert not calls
    router.candidates(agent, include_agents=[agent], now="2026-11-09T12:02:00Z")
    assert len(calls) == 1


@pytest.mark.parametrize("agent", ["codex", "claude", "agy"])
def test_each_provider_records_final_failure(tmp_path, monkeypatch, agent):
    router = Router(project=tmp_path)
    calls = patch_probe(monkeypatch, agent, [AgRouteError("synthetic failure")])
    with pytest.raises(AgRouteError) as exc:
        router.candidates(agent, include_agents=[agent], now=T0)
    assert len(calls) == 2
    assert "retry" in exc.value.details["excluded"][0]["reason"]
    state = router.usage_show(agent, now=T0)["agents"][agent]
    assert state["probe"]["failed_at"] == T0 and state["updated_at"] is None


def test_codex_rpc_timeout_does_not_reuse_cached_quota(tmp_path, monkeypatch):
    import os
    import sys
    import time
    from types import MethodType
    from agroute.adapters import CodexAdapter
    from agroute import usage

    bindir = tmp_path / "fixture-bin"
    bindir.mkdir()
    command = bindir / "codex"
    command.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(10)\n')
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(usage, "PROBE_TIMEOUT_SECONDS", 0.2, raising=False)
    adapter = get_adapter("codex")
    monkeypatch.setattr(adapter, "probe_usage", MethodType(CodexAdapter.probe_usage, adapter))
    monkeypatch.setattr(adapter, "discover_live_usage_patch", lambda: session())
    router = Router(project=tmp_path)
    start = time.monotonic()
    with pytest.raises(AgRouteError) as exc:
        router.candidates("codex", include_agents=["codex"], now=T0)
    assert time.monotonic() - start < 4
    assert "retry" in exc.value.details["excluded"][0]["reason"]
    assert router.usage_show("codex", now=T0)["agents"]["codex"]["updated_at"] is None


@pytest.mark.parametrize("failure", ["timeout", "malformed", "success_without_quota"])
def test_agy_failed_builtin_probe_never_fabricates_available_quota(tmp_path, monkeypatch, failure):
    import subprocess
    from types import MethodType
    from agroute.adapters import AgyAdapter
    import agroute.adapters as adapters

    adapter = get_adapter("agy")
    monkeypatch.setattr(adapter, "probe_usage", MethodType(AgyAdapter.probe_usage, adapter))
    real_which = adapters.shutil.which
    real_run = subprocess.run
    monkeypatch.setattr(adapters.shutil, "which", lambda executable: "/fixture/agy" if executable == "agy" else real_which(executable))
    calls = []
    def run(cmd, **kwargs):
        if cmd[0] != "agy":
            return real_run(cmd, **kwargs)
        calls.append(kwargs["timeout"])
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        output = '{"event":"result","result":{"status":"SUCCESS"}}' if failure == "success_without_quota" else "not a quota report"
        return subprocess.CompletedProcess(cmd, 0, output, "")
    monkeypatch.setattr(subprocess, "run", run)
    router = Router(project=tmp_path)
    with pytest.raises(AgRouteError) as exc:
        router.candidates("agy", include_agents=["agy"], now=T0)
    assert calls == [15, 15]
    assert "retry" in exc.value.details["excluded"][0]["reason"]
    assert router.usage_show("agy", now=T0)["agents"]["agy"]["updated_at"] is None


def test_current_partial_report_retires_already_expired_scope(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    router.usage_set("codex", session_used_pct=97, session_reset_at=T1, now=T0)
    calls = patch_probe(monkeypatch, "codex", [{"weekly_7d": {"used_pct": 5, "reset_at": RESET}}])
    result = router.candidates("codex", include_agents=["codex"], now=T1)
    assert len(calls) == 1 and result["candidates"]
    state = router.usage_show("codex", now=T1)["agents"]["codex"]
    assert state["session_5h"]["used_pct"] is None
    assert state["weekly_7d"]["used_pct"] == 5


def test_current_whole_provider_block_is_a_quota_exclusion(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    router.usage_set("codex", session_used_pct=97, session_reset_at=T1, now=T0)
    calls = patch_probe(monkeypatch, "codex", [{"weekly_7d": {"used_pct": 100, "reset_at": RESET}}])
    with pytest.raises(AgRouteError) as exc:
        router.candidates("codex", include_agents=["codex"], now=T1)
    assert len(calls) == 1
    reason = exc.value.details["excluded"][0]["reason"]
    assert "weekly_7d" in reason and "incident" not in reason


def test_partial_report_does_not_freshen_legacy_window_timestamp(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    router = Router(project=tmp_path)
    router.usage_set("codex", weekly_used_pct=20, weekly_reset_at=RESET, now=T0)
    path = Path(router.paths()["state_root"]) / "usage.json"
    data = json.loads(path.read_text())
    legacy = data["agents"]["codex"]["weekly_7d"]
    legacy.pop("measured_at", None)
    legacy.pop("observed_at", None)
    path.write_text(json.dumps(data))
    calls = patch_probe(monkeypatch, "codex", [session(5)])
    with pytest.raises(AgRouteError) as exc:
        router.candidates("codex", include_agents=["codex"], now=STALE)
    assert len(calls) == 2 and "retry" in str(exc.value)


def test_old_cached_probe_evidence_is_not_recorded_as_current_success(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    old = {"session_5h": {"used_pct": 10, "reset_at": RESET,
                           "observed_at": T0, "measured_at": T0}}
    calls = patch_probe(monkeypatch, "codex", [old])
    with pytest.raises(AgRouteError):
        router.candidates("codex", include_agents=["codex"], now=STALE)
    assert len(calls) == 2
    state = router.usage_show("codex", now=STALE)["agents"]["codex"]
    assert state["probe"].get("succeeded_at") is None
    assert state["probe"]["failed_at"] == STALE
    assert state["updated_at"] in (None, T0)


def test_discovered_stale_window_survives_partial_refresh(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    discovered = {"session_5h": {"used_pct": 10, "reset_at": RESET, "observed_at": T0, "measured_at": T0},
                  "weekly_7d": {"used_pct": 20, "reset_at": RESET, "observed_at": T0, "measured_at": T0},
                  "updated_at": T0}
    monkeypatch.setattr(get_adapter("codex"), "discover_live_usage_patch", lambda: discovered)
    calls = patch_probe(monkeypatch, "codex", [session(5)])
    assert router.usage_show("codex", now=STALE)["agents"]["codex"]["weekly_7d"]["used_pct"] == 20
    with pytest.raises(AgRouteError) as exc:
        router.candidates("codex", include_agents=["codex"], now=STALE)
    assert len(calls) == 2 and "retry" in str(exc.value)


def test_timestamped_old_command_report_is_not_current_evidence(tmp_path):
    import json
    import sys

    report = {"timestamp": T0, "payload": {"rate_limits": {"primary": {"used_percent": 10, "reset_at": RESET}}}}
    script = tmp_path / "old-report.py"
    script.write_text("print(" + repr(json.dumps(report)) + ")\n")
    config = tmp_path / "agroute.toml"
    config.write_text("[agents.codex]\nusage_probe_cmd = " + json.dumps([sys.executable, str(script)]) + "\n")
    router = Router(config, project=tmp_path)
    with pytest.raises(AgRouteError) as exc:
        router.candidates("codex", include_agents=["codex"], now=STALE)
    assert "retry" in str(exc.value)
    state = router.usage_show("codex", now=STALE)["agents"]["codex"]
    assert state["probe"].get("succeeded_at") is None


def test_current_global_availability_restores_provider(tmp_path, monkeypatch):
    router = Router(project=tmp_path)
    router.usage_set("codex", rate_limited=True, now=T0)
    calls = patch_probe(monkeypatch, "codex", [{"rate_limited": False}])
    result = router.candidates("codex", include_agents=["codex"], now=STALE)
    assert len(calls) == 1 and result["candidates"]
    state = router.usage_show("codex", now=STALE)["agents"]["codex"]
    assert not state["rate_limited"] and state["updated_at"] == STALE


def test_provider_expiring_during_other_refresh_is_rechecked(tmp_path, monkeypatch):
    from datetime import datetime
    import importlib
    from agroute import usage

    router = Router(project=tmp_path)
    router.usage_set("codex", session_used_pct=99, session_reset_at=T1, now=T0)
    clock = [datetime.fromisoformat("2026-11-09T12:09:59+00:00")]
    def now(value=None):
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else clock[0]
    monkeypatch.setattr(usage, "_now_utc", now)
    monkeypatch.setattr(importlib.import_module("agroute.router"), "_now_utc", now)
    codex_calls = patch_probe(monkeypatch, "codex", [session(99)])
    def claude_probe(cwd):
        clock[0] = datetime.fromisoformat(T1.replace("Z", "+00:00"))
        return session()
    monkeypatch.setattr(get_adapter("claude"), "probe_usage", claude_probe)
    result = router.candidates("codex", include_agents=["codex", "claude"])
    assert len(codex_calls) == 1
    assert not any(c["model"].startswith("codex/") for c in result["candidates"])
    assert any(c["model"].startswith("claude/") for c in result["candidates"])


def test_malformed_codex_rpc_does_not_abort_healthy_provider(tmp_path, monkeypatch):
    import json
    import os
    import sys
    from types import MethodType
    from agroute.adapters import CodexAdapter

    router = Router(project=tmp_path)
    router.usage_set("claude", session_used_pct=10, session_reset_at=RESET, now=STALE)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    executable = bindir / "codex"
    malformed = {"id": 2, "result": {"rateLimits": {"primary": {"usedPercent": 10, "windowDurationMins": "invalid"}}}}
    executable.write_text(f"#!{sys.executable}\nimport sys\nfor _ in range(3): sys.stdin.readline()\nprint({json.dumps(malformed)!r}, flush=True)\n")
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    adapter = get_adapter("codex")
    monkeypatch.setattr(adapter, "probe_usage", MethodType(CodexAdapter.probe_usage, adapter))
    result = router.candidates("codex", include_agents=["codex", "claude"], now=STALE)
    assert any(c["model"].startswith("claude/") for c in result["candidates"])
    assert not any(c["model"].startswith("codex/") for c in result["candidates"])
    assert any("retry" in e["reason"] for e in result["excluded"] if e["agent"] == "codex")


def test_expiring_refreshes_share_two_attempt_budget(tmp_path, monkeypatch):
    from datetime import datetime
    import importlib
    from agroute import usage

    clock = [datetime.fromisoformat("2026-11-09T12:09:58+00:00")]
    def now(value=None):
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else clock[0]
    monkeypatch.setattr(usage, "_now_utc", now)
    monkeypatch.setattr(importlib.import_module("agroute.router"), "_now_utc", now)
    router = Router(project=tmp_path)
    calls = []
    def codex_probe(cwd):
        calls.append(cwd)
        if len(calls) == 1:
            return session(10, "2026-11-09T12:09:59Z")
        clock[0] = datetime.fromisoformat("2026-11-09T12:10:00+00:00")
        return session(10, T1)
    def claude_probe(cwd):
        clock[0] = datetime.fromisoformat("2026-11-09T12:09:59+00:00")
        return session()
    monkeypatch.setattr(get_adapter("codex"), "probe_usage", codex_probe)
    monkeypatch.setattr(get_adapter("claude"), "probe_usage", claude_probe)
    result = router.candidates("codex", include_agents=["codex", "claude"])
    assert len(calls) == 2
    assert not any(c["model"].startswith("codex/") for c in result["candidates"])
    assert any(c["model"].startswith("claude/") for c in result["candidates"])
    assert any("incident" in e["reason"] and "retry" in e["reason"] for e in result["excluded"] if e["agent"] == "codex")


def test_unrelated_success_does_not_refresh_old_availability(tmp_path):
    import json
    import sys

    report = [
        {"timestamp": T0, "rate_limited": False},
        {"timestamp": STALE, "event": "result", "result": {"status": "SUCCESS"}},
    ]
    script = tmp_path / "stale-with-success.py"
    script.write_text("\n".join("print(" + repr(json.dumps(event)) + ")" for event in report) + "\n")
    config = tmp_path / "agroute.toml"
    config.write_text("[agents.codex]\nusage_probe_cmd = " + json.dumps([sys.executable, str(script)]) + "\n")
    router = Router(config, project=tmp_path)
    router.usage_set("codex", rate_limited=True, now=T0)
    with pytest.raises(AgRouteError) as exc:
        router.candidates("codex", include_agents=["codex"], now=STALE)
    assert "retry" in str(exc.value)
    state = router.usage_show("codex", now=STALE)["agents"]["codex"]
    assert state["probe"].get("succeeded_at") is None
    assert state["updated_at"] == T0

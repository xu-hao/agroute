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

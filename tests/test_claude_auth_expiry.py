"""Behavioral tests for expired and logged-out Claude CLI authentication (#107)."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys
from types import MethodType

import pytest

from agroute import AgRouteError, Router
from agroute.adapters import ClaudeAdapter, get_adapter
from agroute.cli import main as cli_main

T0 = "2026-11-21T12:00:00Z"
T0_PLUS_10S = "2026-11-21T12:00:10Z"
T1 = "2026-11-21T12:05:00Z"
T_STALE = "2026-11-21T12:35:00Z"
T_LATER = "2026-11-21T12:40:00Z"
RESET_5H = "2026-11-21T17:00:00Z"
RESET_7D = "2026-11-28T00:00:00Z"


def _valid_usage_report(session_pct: float = 12.0, weekly_pct: float = 24.0) -> dict:
    return {
        "type": "assistant",
        "usage_report": {
            "rate_limits": {
                "limits": [
                    {
                        "kind": "session",
                        "group": "session",
                        "percent": session_pct,
                        "resets_at": RESET_5H,
                        "scope": None,
                    },
                    {
                        "kind": "weekly_all",
                        "group": "weekly",
                        "percent": weekly_pct,
                        "resets_at": RESET_7D,
                        "scope": None,
                    },
                ]
            }
        },
    }


def _install_fake_claude(
    tmp_path: Path,
    monkeypatch,
    *,
    auth_status_code: int = 1,
    auth_status_stdout: str = "",
    auth_text_stdout: str = "",
    usage_stdout: str = "",
    usage_code: int = 0,
):
    """Install a fake `claude` CLI binary on PATH and restore real ClaudeAdapter.probe_usage."""
    bindir = tmp_path / "fake-bin"
    bindir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "claude-calls.jsonl"
    state_path = tmp_path / "claude-stub-state.json"
    state_path.write_text(
        json.dumps(
            {
                "auth_status_code": auth_status_code,
                "auth_status_stdout": auth_status_stdout,
                "auth_text_stdout": auth_text_stdout,
                "usage_stdout": usage_stdout,
                "usage_code": usage_code,
            }
        ),
        encoding="utf-8",
    )
    fake_claude = bindir / "claude"
    fake_claude.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        f"log_path = Path({str(log_path)!r})\n"
        f"state = json.loads(Path({str(state_path)!r}).read_text(encoding='utf-8'))\n"
        "args = sys.argv[1:]\n"
        "with log_path.open('a', encoding='utf-8') as fh:\n"
        "    fh.write(json.dumps(args) + '\\n')\n"
        "if args[:2] == ['auth', 'status']:\n"
        "    if '--text' in args:\n"
        "        sys.stdout.write(state['auth_text_stdout'])\n"
        "        sys.exit(state['auth_status_code'])\n"
        "    sys.stdout.write(state['auth_status_stdout'])\n"
        "    sys.exit(state['auth_status_code'])\n"
        "if '-p' in args and '/usage' in args:\n"
        "    sys.stdout.write(state['usage_stdout'])\n"
        "    sys.exit(state['usage_code'])\n"
        "sys.exit(2)\n",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    adapter = get_adapter("claude")
    monkeypatch.setattr(adapter, "probe_usage", MethodType(ClaudeAdapter.probe_usage, adapter))
    return log_path, state_path


def _read_calls(log_path: Path) -> list[list[str]]:
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_claude_transcript(lines: list[dict], *, name: str = "session.jsonl") -> Path:
    projects = Path(os.environ["CLAUDE_CONFIG_DIR"]) / "projects" / "fixture-proj"
    projects.mkdir(parents=True, exist_ok=True)
    path = projects / name
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _write_multi_agent_config(tmp_path: Path, *, extra_claude: str = "") -> Path:
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(
        "[agents.agy]\n"
        'cli_candidates = [{ model = "gemini-2.5-pro", effort = "high", tier = "pro" }]\n'
        'native_candidates = [{ model = "gemini-2.5-pro", effort = "high", tier = "pro" }]\n'
        "[agents.claude]\n"
        'cli_candidates = [{ model = "claude-sonnet-4-6", effort = "high", tier = "sonnet" }]\n'
        'native_candidates = [{ model = "claude-sonnet-4-6", effort = "high", tier = "sonnet" }]\n'
        f"{extra_claude}",
        encoding="utf-8",
    )
    return cfg


# ---------------------------------------------------------------------------
# 1. ClaudeAdapter.probe_usage and agr usage probe
# ---------------------------------------------------------------------------


def test_probe_expired_login_via_auth_status_and_text_skips_usage_command(tmp_path, monkeypatch, capsys):
    log_path, _ = _install_fake_claude(
        tmp_path,
        monkeypatch,
        auth_status_code=1,
        auth_status_stdout=json.dumps({"loggedIn": False, "authMethod": "none", "apiProvider": "firstParty"}),
        auth_text_stdout="Login: Expired — log in again\n",
        usage_stdout=json.dumps(_valid_usage_report()),
    )
    router = Router(project=tmp_path)

    with pytest.raises(AgRouteError) as exc_info:
        router.usage_probe("claude", now=T0)

    from agroute import AgRouteAuthError

    exc = exc_info.value
    assert isinstance(exc, AgRouteAuthError)
    assert isinstance(exc, AgRouteError)
    assert exc.auth_status == "expired"
    assert "expired" in str(exc).lower()
    assert "claude auth login" in str(exc).lower()
    calls = _read_calls(log_path)
    assert not any("/usage" in call for call in calls)
    assert any(call[:2] == ["auth", "status"] for call in calls)

    # CLI boundary `agr usage probe --agent claude --json` exits 1 with actionable message.
    monkeypatch.chdir(tmp_path)
    rc = cli_main(
        [
            "usage",
            "probe",
            "--agent",
            "claude",
            "--state-dir",
            router.paths()["state_root"],
            "--no-user-config",
            "--now",
            T0,
            "--json",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err.lower()
    assert "expired" in err and "claude auth login" in err


@pytest.mark.parametrize(
    "status_payload",
    [
        {"loggedIn": False, "authMethod": "none", "loginEnded": True},
        {"loggedIn": True, "authMethod": "oauth", "loginExpiresAt": "2026-11-21T11:00:00Z"},
    ],
)
def test_probe_expired_login_via_json_fields_skips_usage_command(tmp_path, monkeypatch, status_payload):
    log_path, _ = _install_fake_claude(
        tmp_path,
        monkeypatch,
        auth_status_code=1 if not status_payload.get("loggedIn") else 0,
        auth_status_stdout=json.dumps(status_payload),
        usage_stdout=json.dumps(_valid_usage_report()),
    )
    router = Router(project=tmp_path)

    with pytest.raises(AgRouteError) as exc_info:
        router.usage_probe("claude", now=T0)

    from agroute import AgRouteAuthError

    exc = exc_info.value
    assert isinstance(exc, AgRouteAuthError)
    assert exc.auth_status == "expired"
    assert "expired" in str(exc).lower()
    assert "claude auth login" in str(exc).lower()
    assert not any("/usage" in call for call in _read_calls(log_path))


def test_probe_logged_out_raises_auth_error_without_spawning_usage(tmp_path, monkeypatch):
    log_path, _ = _install_fake_claude(
        tmp_path,
        monkeypatch,
        auth_status_code=1,
        auth_status_stdout=json.dumps({"loggedIn": False, "authMethod": "none"}),
        auth_text_stdout="Not logged in. Run claude auth login to authenticate.\n",
        usage_stdout=json.dumps(_valid_usage_report()),
    )
    router = Router(project=tmp_path)

    with pytest.raises(AgRouteError) as exc_info:
        router.usage_probe("claude", now=T0)

    from agroute import AgRouteAuthError

    exc = exc_info.value
    assert isinstance(exc, AgRouteAuthError)
    assert exc.auth_status == "logged_out"
    assert "not logged in" in str(exc).lower()
    assert "claude auth login" in str(exc).lower()
    assert not any("/usage" in call for call in _read_calls(log_path))


def test_probe_stream_json_unauthenticated_fallback_vs_api_key(tmp_path, monkeypatch):
    # 1. Custom usage_probe_cmd returning stream-json with apiKeySource: "none" and local cost text only.
    unauth_stream = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "apiKeySource": "none", "served_by_anthropic": True}),
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "local_command": "usage",
                    "result": "Estimated cost:        $0.0000\n",
                }
            ),
        ]
    )
    probe_script = tmp_path / "unauth_probe.py"
    probe_script.write_text(f"print({unauth_stream!r})\n", encoding="utf-8")
    cfg = tmp_path / "agroute.toml"
    cfg.write_text(f"[agents.claude]\nusage_probe_cmd = {json.dumps([sys.executable, str(probe_script)])}\n")
    router = Router(cfg, project=tmp_path)

    with pytest.raises(AgRouteError) as exc_info:
        router.usage_probe("claude", now=T0)

    from agroute import AgRouteAuthError

    assert isinstance(exc_info.value, AgRouteAuthError)
    assert "claude auth login" in str(exc_info.value).lower()

    # 2. Built-in probe when auth status produces no JSON and /usage emits apiKeySource: "ANTHROPIC_API_KEY".
    api_key_stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "system",
                    "subtype": "init",
                    "apiKeySource": "ANTHROPIC_API_KEY",
                    "served_by_anthropic": True,
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "local_command": "usage",
                    "result": "Estimated cost:        $0.0000\n",
                }
            ),
        ]
    )
    builtin_proj = tmp_path / "builtin"
    builtin_proj.mkdir()
    _install_fake_claude(
        builtin_proj,
        monkeypatch,
        auth_status_code=1,
        auth_status_stdout="not json\n",
        usage_stdout=api_key_stream + "\n",
    )
    router_builtin = Router(project=builtin_proj)
    with pytest.raises(AgRouteError) as api_exc_info:
        router_builtin.usage_probe("claude", now=T0)
    assert not isinstance(api_exc_info.value, AgRouteAuthError)
    assert "expired" not in str(api_exc_info.value).lower()

    # 3. Built-in probe when auth status says authMethod == "api_key".
    _install_fake_claude(
        builtin_proj,
        monkeypatch,
        auth_status_code=0,
        auth_status_stdout=json.dumps({"loggedIn": True, "authMethod": "api_key"}),
        usage_stdout=api_key_stream + "\n",
    )
    with pytest.raises(AgRouteError) as method_exc_info:
        router_builtin.usage_probe("claude", now=T0)
    assert not isinstance(method_exc_info.value, AgRouteAuthError)
    assert "api key" in str(method_exc_info.value).lower()
    assert "expired" not in str(method_exc_info.value).lower()


# ---------------------------------------------------------------------------
# 2. Routing freshness, bounded single attempt, and immediate recovery
# ---------------------------------------------------------------------------


def test_candidates_single_attempt_exclusion_and_immediate_recovery(tmp_path, monkeypatch):
    cfg = _write_multi_agent_config(tmp_path)
    log_path, state_path = _install_fake_claude(
        tmp_path,
        monkeypatch,
        auth_status_code=1,
        auth_status_stdout=json.dumps({"loggedIn": False, "authMethod": "none", "loginEnded": True}),
        auth_text_stdout="Login: Expired — log in again\n",
    )
    router = Router(cfg, project=tmp_path)
    router.usage_set("agy", session_used_pct=10.0, weekly_used_pct=10.0, now=T0)

    # Multi-agent routing: probes Claude once, admits agy, excludes claude with actionable reason.
    res = router.candidates("agy", include_agents=["agy", "claude"], now=T0)
    assert len(_read_calls(log_path)) == 1
    assert any(c["model"].startswith("agy/") for c in res["candidates"])
    assert not any(c["model"].startswith("claude/") for c in res["candidates"])

    claude_excl = [e for e in res["excluded"] if e["agent"] == "claude"]
    assert len(claude_excl) == 1
    reason = claude_excl[0]["reason"]
    assert "claude auth login" in reason.lower()
    assert "after 2 attempts" not in reason.lower()
    assert "retry with agr usage probe --agent claude" not in reason.lower()
    assert any("claude auth login" in w.lower() for w in res["warnings"])

    # Single-agent routing fails closed on candidates, inventory, and plan with structured exclusions.
    for boundary in ("candidates", "inventory", "plan"):
        before_count = len(_read_calls(log_path))
        with pytest.raises(AgRouteError) as exc_info:
            if boundary == "candidates":
                router.candidates("claude", include_agents=["claude"], now=T0)
            elif boundary == "inventory":
                router.inventory("claude", include_agents=["claude"], now=T0)
            else:
                router.plan(
                    "claude",
                    {
                        "decision_id": "dec-auth-blocked",
                        "chosen": {"model": "claude/claude-sonnet-4-6", "effort": "high"},
                    },
                    include_agents=["claude"],
                    now=T0,
                )
        after_count = len(_read_calls(log_path))
        assert after_count - before_count == 1
        excluded = exc_info.value.details["excluded"]
        assert any(e["agent"] == "claude" and "claude auth login" in e["reason"].lower() for e in excluded)

    # Immediate recovery 10 seconds later (well within 1800s stale window): no 30-minute lock!
    state_path.write_text(
        json.dumps(
            {
                "auth_status_code": 0,
                "auth_status_stdout": json.dumps(
                    {
                        "loggedIn": True,
                        "authMethod": "oauth",
                        "planTier": "max_20x",
                        "subscriptionType": "max",
                    }
                ),
                "auth_text_stdout": "",
                "usage_stdout": json.dumps(_valid_usage_report(15.0, 20.0)) + "\n",
                "usage_code": 0,
            }
        ),
        encoding="utf-8",
    )
    recovered = router.candidates("claude", include_agents=["claude"], now=T0_PLUS_10S)
    assert any(c["model"] == "claude/claude-sonnet-4-6" for c in recovered["candidates"])
    show = router.usage_show("claude", now=T0_PLUS_10S)["agents"]["claude"]
    assert not show.get("auth", {}).get("status")
    assert "failed_at" not in show.get("probe", {})
    assert show["detected_plan"] == "max-20x"
    assert show["eligible_as_target"] is True


# ---------------------------------------------------------------------------
# 3. Eligibility in Router.usage_show() and agr tui
# ---------------------------------------------------------------------------


def test_usage_show_marks_claude_ineligible_after_stale_and_fresh_auth_failure(tmp_path, monkeypatch):
    cfg = _write_multi_agent_config(tmp_path)
    _, state_path = _install_fake_claude(
        tmp_path,
        monkeypatch,
        auth_status_code=0,
        auth_status_stdout=json.dumps({"loggedIn": True, "authMethod": "oauth", "planTier": "max_20x"}),
        usage_stdout=json.dumps(_valid_usage_report(10.0, 100.0)) + "\n",
    )
    router = Router(cfg, project=tmp_path)
    router.usage_probe("claude", now=T0)

    # Login expires at T_STALE (> 1800s later): visible_agent_state clears stale weekly_7d to None,
    # yet usage_show must still report Claude and all its models ineligible!
    state_path.write_text(
        json.dumps(
            {
                "auth_status_code": 1,
                "auth_status_stdout": json.dumps({"loggedIn": False, "authMethod": "none", "loginEnded": True}),
                "auth_text_stdout": "Login: Expired — log in again\n",
                "usage_stdout": "",
                "usage_code": 1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AgRouteError):
        router.usage_probe("claude", now=T_STALE)

    stale_snap = router.usage_show("claude", now=T_STALE)["agents"]["claude"]
    assert stale_snap["weekly_7d"]["used_pct"] is None
    assert stale_snap["eligible_as_target"] is False
    assert stale_snap["eligible_as_host_native"] is False
    assert "claude auth login" in (stale_snap["target_exclusion_reason"] or "").lower()
    assert "claude auth login" in (stale_snap["host_native_exclusion_reason"] or "").lower()
    assert stale_snap["models"]
    for model_info in stale_snap["models"].values():
        assert model_info["eligible"] is False
        assert model_info["native_eligible"] is False
        assert "claude auth login" in (model_info["exclusion_reason"] or "").lower()
        assert "claude auth login" in (model_info["native_exclusion_reason"] or "").lower()

    # Even if fresh counters (session_5h = 10%) were recorded 1 minute earlier, active auth failure
    # still marks claude and its models ineligible.
    router_fresh = Router(cfg, project=tmp_path, state_directory=tmp_path / "fresh-state", include_user_config=False)
    router_fresh.usage_set("claude", session_used_pct=10.0, session_reset_at=RESET_5H, now=T0)
    with pytest.raises(AgRouteError):
        router_fresh.usage_probe("claude", now=T0_PLUS_10S)
    fresh_snap = router_fresh.usage_show("claude", now=T0_PLUS_10S)["agents"]["claude"]
    assert fresh_snap["session_5h"]["used_pct"] == 10.0
    assert fresh_snap["eligible_as_target"] is False
    assert fresh_snap["eligible_as_host_native"] is False
    assert "claude auth login" in (fresh_snap["target_exclusion_reason"] or "").lower()
    assert all(
        (not m["eligible"]) and (not m["native_eligible"])
        for m in fresh_snap["models"].values()
    )


@pytest.mark.asyncio
async def test_tui_shows_claude_blocked_on_expired_login_and_recovers_on_probe(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import DataTable, Static
    from agroute.tui import build_app

    cfg = _write_multi_agent_config(tmp_path)
    _, state_path = _install_fake_claude(
        tmp_path,
        monkeypatch,
        auth_status_code=1,
        auth_status_stdout=json.dumps({"loggedIn": False, "authMethod": "none", "loginEnded": True}),
        auth_text_stdout="Login: Expired — log in again\n",
    )
    router = Router(cfg, project=tmp_path, state_directory=tmp_path / "tui-state", include_user_config=False)
    router.usage_set("codex", session_used_pct=10.0, weekly_used_pct=10.0, now=T0)
    router.usage_set("agy", session_used_pct=10.0, weekly_used_pct=10.0, now=T0)
    with pytest.raises(AgRouteError):
        router.usage_probe("claude", now=T0)

    # Keep codex/agy healthy during P probe without invoking real codex/agy CLIs.
    monkeypatch.setattr(get_adapter("codex"), "probe_usage", lambda cwd: {"session_5h": {"used_pct": 10.0, "reset_at": RESET_5H}})
    monkeypatch.setattr(get_adapter("agy"), "probe_usage", lambda cwd: {"session_5h": {"used_pct": 10.0, "reset_at": RESET_5H}})

    now_holder = [T0_PLUS_10S]
    app = build_app(
        lambda: router.snapshot(all_repos=False, now=now_holder[0]),
        probe_factory=lambda manual: router.tui_probe(manual, now=now_holder[0]),
        refresh_interval=60.0,
    )
    async with app.run_test(size=(140, 32)) as pilot:
        await app.workers.wait_for_complete()
        await pilot.pause()

        status_txt = str(app.query_one("#route-status", Static).render())
        assert "claude BLOCKED" in status_txt
        assert "claude OK" not in status_txt

        # Switch to QUOTAS view (`u`) and check #quotas rows and #route-detail for claude.
        await pilot.press("u")
        await pilot.pause()
        quotas = app.query_one("#quotas", DataTable)
        claude_rows = [
            (str(r.key.value), quotas.get_row(r.key.value))
            for r in quotas.ordered_rows
            if str(r.key.value).startswith("claude:")
        ]
        assert claude_rows
        for _key, cells in claude_rows:
            assert cells[3].plain == "BLOCKED"

        # Highlight the first claude row in #quotas and check #route-detail.
        first_claude_idx = next(
            idx for idx, r in enumerate(quotas.ordered_rows) if str(r.key.value).startswith("claude:")
        )
        quotas.move_cursor(row=first_claude_idx)
        await pilot.pause()
        detail_txt = str(app.query_one("#route-detail", Static).render())
        assert "claude auth login" in detail_txt.lower()

        # Simulate re-login and press 'P' (manual probe).
        state_path.write_text(
            json.dumps(
                {
                    "auth_status_code": 0,
                    "auth_status_stdout": json.dumps(
                        {"loggedIn": True, "authMethod": "oauth", "planTier": "max_20x"}
                    ),
                    "auth_text_stdout": "",
                    "usage_stdout": json.dumps(_valid_usage_report(15.0, 20.0)) + "\n",
                    "usage_code": 0,
                }
            ),
            encoding="utf-8",
        )
        now_holder[0] = T1
        await pilot.press("P")
        await app.workers.wait_for_complete()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

        status_after = str(app.query_one("#route-status", Static).render())
        assert "claude OK" in status_after
        assert "claude BLOCKED" not in status_after
        for _key, cells in [
            (str(r.key.value), quotas.get_row(r.key.value))
            for r in quotas.ordered_rows
            if str(r.key.value).startswith("claude:")
        ]:
            assert cells[3].plain == "OK"
        detail_after = str(app.query_one("#route-detail", Static).render())
        assert "target_eligible: True (eligible)" in detail_after


# ---------------------------------------------------------------------------
# 4. parse_native_usage (usage_ingest) and passive transcript discovery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw_input", "expected_status"),
    [
        (
            json.dumps(
                {
                    "type": "assistant",
                    "error": "authentication_failed",
                    "message": {
                        "role": "assistant",
                        "model": "<synthetic>",
                        "content": [
                            {
                                "type": "text",
                                "text": "Failed to authenticate: this login reached its time limit. Run claude auth login",
                            }
                        ],
                    },
                }
            ),
            "expired",
        ),
        (
            json.dumps(
                {
                    "type": "result",
                    "is_error": True,
                    "api_error_status": 401,
                    "result": "API Error: 401 Unauthorized - please run /login",
                }
            ),
            "logged_out",
        ),
        (
            json.dumps({"loggedIn": False, "authMethod": "none", "loginEnded": True}),
            "expired",
        ),
        (
            "Login: Expired — log in again",
            "expired",
        ),
        (
            "Not logged in. Run claude auth login to authenticate.",
            "logged_out",
        ),
    ],
)
def test_usage_ingest_auth_failure_and_immediate_recovery(tmp_path, monkeypatch, raw_input, expected_status):
    cfg = _write_multi_agent_config(tmp_path)
    _, _ = _install_fake_claude(
        tmp_path,
        monkeypatch,
        auth_status_code=0,
        auth_status_stdout=json.dumps({"loggedIn": True, "authMethod": "oauth", "planTier": "max_20x"}),
        usage_stdout=json.dumps(_valid_usage_report(5.0, 10.0)) + "\n",
    )
    router = Router(cfg, project=tmp_path)

    res = router.usage_ingest("claude", raw_input, now=T0)
    assert res["usage"]["auth"]["status"] == expected_status
    assert res["usage"]["rate_limited"] is False
    assert res["usage"]["availability"].get("status") != "rejected"

    show = router.usage_show("claude", now=T0)["agents"]["claude"]
    assert show["eligible_as_target"] is False
    assert show["eligible_as_host_native"] is False
    assert "claude auth login" in (show["target_exclusion_reason"] or "").lower()

    # Immediate routing re-probe clears ingested auth failure once logged back in.
    cands = router.candidates("claude", include_agents=["claude"], now=T0_PLUS_10S)
    assert any(c["model"] == "claude/claude-sonnet-4-6" for c in cands["candidates"])
    recovered = router.usage_show("claude", now=T0_PLUS_10S)["agents"]["claude"]
    assert not recovered.get("auth", {}).get("status")
    assert recovered["eligible_as_target"] is True


def test_usage_ingest_plan_only_still_errors_and_429_still_records_quota_rejection(tmp_path):
    router = Router(project=tmp_path)

    with pytest.raises(AgRouteError, match="no recognizable native usage"):
        router.usage_ingest("claude", json.dumps({"planTier": "max_20x", "subscriptionType": "max"}), now=T0)

    res_429 = router.usage_ingest(
        "claude",
        json.dumps(
            {
                "type": "result",
                "is_error": True,
                "api_error_status": 429,
                "result": "You've hit your weekly limit · resets 2026-11-16T00:00:00Z",
            }
        ),
        now=T0,
    )
    assert not res_429["usage"].get("auth", {}).get("status")
    assert res_429["usage"]["weekly_7d"]["status"] == "rejected"


def test_passive_transcript_auth_failure_supersession_and_touch_non_replay(tmp_path, monkeypatch):
    cfg = _write_multi_agent_config(tmp_path)
    _, _ = _install_fake_claude(
        tmp_path,
        monkeypatch,
        auth_status_code=0,
        auth_status_stdout=json.dumps({"loggedIn": True, "authMethod": "oauth", "planTier": "max_20x"}),
        usage_stdout=json.dumps(_valid_usage_report(8.0, 12.0)) + "\n",
    )
    router = Router(cfg, project=tmp_path)

    # 1. Untimestamped authentication_failed transcript marks claude ineligible offline.
    auth_err_entry = {
        "type": "assistant",
        "error": "authentication_failed",
        "message": {
            "role": "assistant",
            "model": "<synthetic>",
            "content": [{"type": "text", "text": "Failed to authenticate: this login reached its time limit."}],
        },
    }
    path = _write_claude_transcript([auth_err_entry])
    t1_ts = datetime.fromisoformat(T1.replace("Z", "+00:00")).timestamp()
    os.utime(path, (t1_ts, t1_ts))

    offline = router.usage_show("claude", now=T1)["agents"]["claude"]
    assert offline["eligible_as_target"] is False
    assert offline["rate_limited"] is False
    assert "claude auth login" in (offline["target_exclusion_reason"] or "").lower()

    # 2. A newer valid assistant turn in the transcript clears the discovered auth patch offline.
    valid_turn = {
        "type": "assistant",
        "timestamp": T_STALE,
        "message": {
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "Ready"}],
        },
    }
    _write_claude_transcript([auth_err_entry, valid_turn], name="session-recovered.jsonl")
    cleared_offline = router.usage_show("claude", now=T_STALE)["agents"]["claude"]
    assert cleared_offline["eligible_as_target"] is True

    # An explicit "allowed" rate_limit_event also supersedes an earlier transcript auth failure.
    allowed_rl = {
        "type": "rate_limit_event",
        "timestamp": T_STALE,
        "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": RESET_5H},
    }
    _write_claude_transcript([auth_err_entry, allowed_rl], name="session-recovered.jsonl")
    assert router.usage_show("claude", now=T_STALE)["agents"]["claude"]["eligible_as_target"] is True

    # Remove the second transcript, keep only the untimestamped auth failure transcript,
    # run a successful usage_probe, then touch the old transcript file later:
    # the old event must NOT replay!
    (path.parent / "session-recovered.jsonl").unlink()
    assert router.usage_show("claude", now=T1)["agents"]["claude"]["eligible_as_target"] is False

    router.usage_probe("claude", now=T_STALE)
    assert router.usage_show("claude", now=T_STALE)["agents"]["claude"]["eligible_as_target"] is True

    # Touch old transcript with a dummy line (bumping mtime to T_LATER > T_STALE).
    path.write_text(
        path.read_text(encoding="utf-8") + json.dumps({"type": "assistant", "message": "unrelated fixture"}) + "\n",
        encoding="utf-8",
    )
    t_later_ts = datetime.fromisoformat(T_LATER.replace("Z", "+00:00")).timestamp()
    os.utime(path, (t_later_ts, t_later_ts))

    after_touch = router.usage_show("claude", now=T_LATER)["agents"]["claude"]
    assert after_touch["eligible_as_target"] is True

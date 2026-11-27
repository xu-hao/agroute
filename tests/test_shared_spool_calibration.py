"""Account-wide quota-credit calibration shared across repositories through the spool (#129)."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import select
import subprocess
import sys

import pytest

from agroute import AgRouteError, Router
from agroute.calibration import read_calibration_store, write_calibration_store
from agroute.routes import register_repository
from agroute.state import claim

SESSION_KEY = "codex:plus-20:shared:session_5h"
WEEKLY_KEY = "codex:plus-20:shared:weekly_7d"
CLAUDE_KEY = "claude:pro-20:shared:session_5h"
SESSION_RESET = "2026-11-12T17:00:00Z"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("HOWHARD_STATE_DIR", raising=False)
    monkeypatch.delenv("AGROUTE_DISABLE_SPOOL", raising=False)


def _spool() -> Path:
    return Path(os.environ["AGROUTE_SPOOL_DIR"])


def _git_repo(tmp_path: Path, name: str) -> tuple[Path, Path]:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, capture_output=True, timeout=30)
    cfg = repo / "agroute.toml"
    cfg.write_text('[agents.codex]\nplan = "plus-20"\nhost_reserve_pct = 0.0\n', encoding="utf-8")
    return repo, cfg


def _router(repo: Path, cfg: Path, **kwargs) -> Router:
    return Router(cfg, project=repo, include_user_config=False, **kwargs)


def _set_session(router: Router, used_pct: float, now: str) -> None:
    router.usage_set("codex", session_used_pct=used_pct, session_reset_at=SESSION_RESET, now=now)


def _append_observation(repo: Path, obs_id: str, observed_at: str, credit: float) -> None:
    path = repo / ".git" / "howhard" / "outcomes.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": obs_id,
        "model": "codex/gpt-5.4",
        "effort": "high",
        "outcome": "clean",
        "observed_at": observed_at,
        "needed_credit_usd": credit,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")


def _session_calibration(router: Router, now: str) -> dict:
    agent = router.usage_show(agent="codex", now=now)["agents"]["codex"]
    return agent["credit_calibration"]["shared"]["session_5h"]


def _pool(
    key: str,
    *,
    s_credit: float,
    s_frac: float,
    matched: int,
    updated_at: str,
    snapshot_at: str | None,
    rejected: int = 0,
    used_pct: float = 10.0,
) -> dict:
    agent, plan, group, window = key.split(":")
    snapshot = None
    if snapshot_at is not None:
        snapshot = {"observed_at": snapshot_at, "used_pct": used_pct, "reset_at": SESSION_RESET}
    return {
        "agent": agent,
        "plan": plan,
        "quota_group": group,
        "window": window,
        "s_credit": s_credit,
        "s_frac": s_frac,
        "matched_intervals": matched,
        "rejected_outliers": rejected,
        "updated_at": updated_at,
        "last_snapshot": snapshot,
    }


def _write_legacy(router: Router, pools: dict) -> Path:
    """Create a claimed per-repository store as written by earlier releases."""
    claim(router.config)
    path = router.config.state_root / "credit_calibration.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "agroute-credit-calibration-v2", "pools": pools}), encoding="utf-8")
    register_repository(router.config)
    return path


def _read_pools(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["pools"]


def _two_legacy_repositories(tmp_path: Path):
    repo_a, cfg_a = _git_repo(tmp_path, "repo-a")
    repo_b, cfg_b = _git_repo(tmp_path, "repo-b")
    router_a, router_b = _router(repo_a, cfg_a), _router(repo_b, cfg_b)
    legacy_a = _write_legacy(
        router_a,
        {
            # Weaker statistics but the newest anchor (fractional seconds sort after whole seconds).
            SESSION_KEY: _pool(
                SESSION_KEY,
                s_credit=3.0,
                s_frac=0.5,
                matched=7,
                updated_at="2026-11-12T11:00:00Z",
                snapshot_at="2026-11-12T11:00:00.500000Z",
                used_pct=12.0,
            ),
            # Equal `s_frac` (as at the forgetting cap): `matched_intervals` decides.
            WEEKLY_KEY: _pool(
                WEEKLY_KEY,
                s_credit=100.0,
                s_frac=2.0,
                matched=9,
                rejected=4,
                updated_at="2026-11-10T09:00:00Z",
                snapshot_at=None,
            ),
        },
    )
    legacy_b = _write_legacy(
        router_b,
        {
            SESSION_KEY: _pool(
                SESSION_KEY,
                s_credit=9.0,
                s_frac=1.0,
                matched=3,
                rejected=1,
                updated_at="2026-11-12T09:00:00Z",
                snapshot_at="2026-11-12T11:00:00Z",
                used_pct=8.0,
            ),
            WEEKLY_KEY: _pool(
                WEEKLY_KEY,
                s_credit=60.0,
                s_frac=2.0,
                matched=5,
                updated_at="2026-11-12T09:00:00Z",
                snapshot_at="2026-11-12T09:00:00Z",
            ),
            CLAUDE_KEY: _pool(
                CLAUDE_KEY,
                s_credit=2.0,
                s_frac=0.1,
                matched=1,
                updated_at="2026-11-12T09:00:00Z",
                snapshot_at="2026-11-12T09:00:00Z",
            ),
        },
    )
    return (repo_a, cfg_a, router_a, legacy_a), (repo_b, cfg_b, router_b, legacy_b)


@pytest.mark.parametrize("spool_env", ["AGROUTE_SPOOL_DIR", "XDG_DATA_HOME"])
def test_calibration_interval_spans_two_repositories(tmp_path, monkeypatch, spool_env):
    if spool_env == "XDG_DATA_HOME":
        monkeypatch.delenv("AGROUTE_SPOOL_DIR")
        spool = tmp_path / "xdg-data" / "agroute"
    else:
        spool = _spool()
    repo_a, cfg_a = _git_repo(tmp_path, "repo-a")
    repo_b, cfg_b = _git_repo(tmp_path, "repo-b")
    router_a, router_b = _router(repo_a, cfg_a), _router(repo_b, cfg_b)
    assert router_a.config.explicit_state is False
    assert router_b.config.explicit_state is False

    _set_session(router_a, 10.0, "2026-11-12T12:00:00Z")
    _append_observation(repo_b, "obs-b-1", "2026-11-12T12:10:00Z", 1.50)
    _set_session(router_b, 15.0, "2026-11-12T12:15:00Z")

    # plus-20 session prior $20: (0.20 * 20 + 1.50) / (0.20 + 0.05) = 22.0
    for router in (router_a, router_b, _router(repo_a, cfg_a)):
        calibration = _session_calibration(router, "2026-11-12T12:15:00Z")
        assert calibration["matched_intervals"] == 1
        assert calibration["calibrated_credit_usd"] == pytest.approx(22.0)

    shared = _read_pools(spool / "credit_calibration.json")
    assert shared[SESSION_KEY]["matched_intervals"] == 1
    assert shared[SESSION_KEY]["last_snapshot"]["used_pct"] == 15.0
    for repo in (repo_a, repo_b):
        assert not (repo / ".git" / "agroute" / "credit_calibration.json").exists()
    # The spool is not a per-project store: it must never be claimed by one repository.
    assert not (spool / "store.json").exists()


def test_clear_in_one_repository_resets_the_shared_anchor(tmp_path):
    repo_a, cfg_a = _git_repo(tmp_path, "repo-a")
    repo_b, cfg_b = _git_repo(tmp_path, "repo-b")
    router_a, router_b = _router(repo_a, cfg_a), _router(repo_b, cfg_b)
    _set_session(router_a, 10.0, "2026-11-12T12:00:00Z")

    router_b.usage_clear(agent="codex")

    assert _read_pools(_spool() / "credit_calibration.json")[SESSION_KEY]["last_snapshot"] is None


def test_shared_store_is_seeded_once_from_legacy_repository_stores_without_modifying_them(tmp_path):
    (_, _, router_a, legacy_a), (_, _, router_b, legacy_b) = _two_legacy_repositories(tmp_path)

    # Damaged legacy evidence must be ignored without failing or hiding valid evidence.
    repo_bad, cfg_bad = _git_repo(tmp_path, "repo-corrupt")
    corrupt = _write_legacy(_router(repo_bad, cfg_bad), {})
    corrupt.write_bytes(b"\xff{not json")
    repo_partial, cfg_partial = _git_repo(tmp_path, "repo-partial")
    partial = _write_legacy(
        _router(repo_partial, cfg_partial),
        {
            "codex:plus-20:shared:bogus": {"agent": "codex"},
            "codex:plus-20:shared:junk": 7,
        },
    )
    repo_unreadable, cfg_unreadable = _git_repo(tmp_path, "repo-unreadable")
    unreadable = _write_legacy(_router(repo_unreadable, cfg_unreadable), {})
    unreadable.unlink()
    unreadable.mkdir()
    registry = _spool() / "repositories.json"
    payload = json.loads(registry.read_text(encoding="utf-8"))
    payload["repositories"] += [
        {"capture_root": str(tmp_path / "gone"), "state_root": str(tmp_path / "gone" / "state")},
        {"capture_root": 3, "state_root": None},
        "junk",
    ]
    registry.write_text(json.dumps(payload), encoding="utf-8")
    originals = {path: path.read_bytes() for path in (legacy_a, legacy_b, corrupt, partial)}

    repo_c, cfg_c = _git_repo(tmp_path, "repo-c")
    router_c = _router(repo_c, cfg_c)
    shared_path = _spool() / "credit_calibration.json"
    for router in (router_a, router_b, router_c):
        agent = router.usage_show(agent="codex", now="2026-11-12T11:30:00Z")["agents"]["codex"]
        shared = agent["credit_calibration"]["shared"]
        assert shared["session_5h"]["matched_intervals"] == 3
        assert shared["session_5h"]["calibrated_credit_usd"] == pytest.approx((0.2 * 20 + 9.0) / 1.2)
        assert shared["weekly_7d"]["matched_intervals"] == 9
    assert not shared_path.exists(), "reading must not persist the seed"

    # The next calibration write persists the merged store.
    write_calibration_store(router_c.config, read_calibration_store(router_c.config))
    persisted = _read_pools(shared_path)
    session = persisted[SESSION_KEY]
    assert (session["s_credit"], session["s_frac"], session["matched_intervals"]) == (9.0, 1.0, 3)
    assert session["rejected_outliers"] == 1
    assert session["updated_at"] == "2026-11-12T09:00:00Z"
    assert session["last_snapshot"] == {
        "observed_at": "2026-11-12T11:00:00.500000Z",
        "used_pct": 12.0,
        "reset_at": SESSION_RESET,
    }
    weekly = persisted[WEEKLY_KEY]
    assert (weekly["s_credit"], weekly["matched_intervals"], weekly["rejected_outliers"]) == (100.0, 9, 4)
    assert weekly["last_snapshot"]["observed_at"] == "2026-11-12T09:00:00Z"
    assert persisted[CLAUDE_KEY]["s_credit"] == 2.0
    assert {path: path.read_bytes() for path in originals} == originals

    # Seeding happens once: later edits to a legacy file no longer reach the shared store.
    changed = json.loads(legacy_a.read_text(encoding="utf-8"))
    changed["pools"][SESSION_KEY].update(s_frac=2.0, matched_intervals=99)
    legacy_a.write_text(json.dumps(changed), encoding="utf-8")
    assert _session_calibration(router_a, "2026-11-12T11:30:00Z")["matched_intervals"] == 3


def test_seeding_includes_an_unregistered_current_repository_with_a_v1_store(tmp_path):
    repo, cfg = _git_repo(tmp_path, "repo-v1")
    router = _router(repo, cfg)
    claim(router.config)
    legacy = router.config.state_root / "credit_calibration.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    entry = _pool(
        SESSION_KEY,
        s_credit=0.0,
        s_frac=0.25,
        matched=1,
        updated_at="2026-11-12T09:00:00Z",
        snapshot_at=None,
    )
    entry["s_cost"] = 8.5
    del entry["s_credit"]
    legacy.write_text(
        json.dumps({"format": "agroute-credit-calibration-v1", "pools": {SESSION_KEY: entry}}),
        encoding="utf-8",
    )
    assert not (_spool() / "repositories.json").exists()

    calibration = _session_calibration(router, "2026-11-12T11:30:00Z")

    assert calibration["matched_intervals"] == 1
    assert calibration["calibrated_credit_usd"] == pytest.approx((0.2 * 20 + 8.5) / 0.45)


def test_usage_clear_is_honored_across_seeded_pools_before_the_shared_store_exists(tmp_path):
    (_, _, router_a, legacy_a), (_, _, _, legacy_b) = _two_legacy_repositories(tmp_path)
    originals = {path: path.read_bytes() for path in (legacy_a, legacy_b)}
    shared_path = _spool() / "credit_calibration.json"
    assert not shared_path.exists()

    router_a.usage_clear(agent="codex")

    persisted = _read_pools(shared_path)
    assert persisted[SESSION_KEY]["last_snapshot"] is None
    assert persisted[SESSION_KEY]["matched_intervals"] == 3
    assert persisted[WEEKLY_KEY]["last_snapshot"] is None
    assert persisted[WEEKLY_KEY]["s_credit"] == 100.0
    assert persisted[CLAUDE_KEY]["last_snapshot"]["observed_at"] == "2026-11-12T09:00:00Z"
    assert {path: path.read_bytes() for path in originals} == originals


@pytest.mark.parametrize("selector", ["state_directory", "AGROUTE_STATE_DIR"])
def test_explicit_state_directory_stays_isolated_from_the_shared_store(tmp_path, monkeypatch, selector):
    (_, _, _, legacy_a), _ = _two_legacy_repositories(tmp_path)
    shared_path = _spool() / "credit_calibration.json"
    shared_pool = _pool(
        SESSION_KEY,
        s_credit=40.0,
        s_frac=2.0,
        matched=50,
        updated_at="2026-11-12T09:00:00Z",
        snapshot_at="2026-11-12T09:00:00Z",
    )
    shared_path.write_text(
        json.dumps({"format": "agroute-credit-calibration-v2", "pools": {SESSION_KEY: shared_pool}}),
        encoding="utf-8",
    )
    originals = {path: path.read_bytes() for path in (shared_path, legacy_a)}

    repo, cfg = _git_repo(tmp_path, "repo-isolated")
    state_dir = tmp_path / "isolated-state"
    if selector == "AGROUTE_STATE_DIR":
        monkeypatch.setenv("AGROUTE_STATE_DIR", str(state_dir))
        router = _router(repo, cfg)
    else:
        router = _router(repo, cfg, state_directory=state_dir)
    assert router.config.explicit_state is True
    assert _session_calibration(router, "2026-11-12T11:30:00Z")["matched_intervals"] == 0

    _set_session(router, 10.0, "2026-11-12T12:00:00Z")
    assert _read_pools(state_dir / "credit_calibration.json")[SESSION_KEY]["matched_intervals"] == 0
    router.usage_clear(agent="codex")
    assert _read_pools(state_dir / "credit_calibration.json")[SESSION_KEY]["last_snapshot"] is None

    assert {path: path.read_bytes() for path in originals} == originals
    assert not (_spool() / "credit_calibration.lock").exists()


def test_explicit_state_directory_still_rejects_an_unclaimed_foreign_store(tmp_path):
    repo, cfg = _git_repo(tmp_path, "repo")
    foreign = tmp_path / "foreign-state"
    foreign.mkdir()
    (foreign / "credit_calibration.json").write_text("{}", encoding="utf-8")

    with pytest.raises(AgRouteError, match="unclaimed"):
        _router(repo, cfg, state_directory=foreign)


def test_disabled_spool_keeps_calibration_in_the_repository_store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGROUTE_DISABLE_SPOOL", "1")
    repo, cfg = _git_repo(tmp_path, "repo")
    router = _router(repo, cfg)

    _set_session(router, 10.0, "2026-11-12T12:00:00Z")

    assert SESSION_KEY in _read_pools(repo / ".git" / "agroute" / "credit_calibration.json")
    assert not (_spool() / "credit_calibration.json").exists()


@pytest.mark.parametrize(
    "operation",
    [
        'router.usage_set("codex", session_used_pct=15.0, '
        'session_reset_at="2026-11-12T17:00:00Z", now="2026-11-12T12:15:00Z")',
        'router.usage_clear(agent="codex")',
    ],
    ids=["record", "clear"],
)
def test_calibration_updates_wait_for_the_cross_process_spool_lock(tmp_path, operation):
    repo, cfg = _git_repo(tmp_path, "repo")
    _set_session(_router(repo, cfg), 10.0, "2026-11-12T12:00:00Z")
    shared_path = _spool() / "credit_calibration.json"
    before = shared_path.read_bytes()
    script = (
        "import sys\n"
        "from agroute import Router\n"
        "router = Router(sys.argv[2], project=sys.argv[1], include_user_config=False)\n"
        "print('ready', flush=True)\n"
        f"{operation}\n"
    )
    child = None
    try:
        with (_spool() / "credit_calibration.lock").open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            child = subprocess.Popen(
                [sys.executable, "-c", script, str(repo), str(cfg)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert select.select([child.stdout], [], [], 30)[0], "child did not start"
            assert child.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                child.wait(timeout=1.5)
            assert shared_path.read_bytes() == before
            fcntl.flock(handle, fcntl.LOCK_UN)
        _, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, stderr
        assert shared_path.read_bytes() != before
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.communicate(timeout=30)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_spool_skips_calibration_without_failing_usage_commands(tmp_path):
    """Sandboxed hosts may expose the user spool read-only; calibration stays best-effort there."""
    repo, cfg = _git_repo(tmp_path, "repo")
    router = _router(repo, cfg)
    _set_session(router, 10.0, "2026-11-12T12:00:00Z")
    spool = _spool()
    shared_path = spool / "credit_calibration.json"
    before = shared_path.read_bytes()
    (spool / "credit_calibration.lock").unlink(missing_ok=True)
    mode = spool.stat().st_mode
    try:
        spool.chmod(0o555)
        _set_session(router, 15.0, "2026-11-12T12:15:00Z")
        assert _session_calibration(router, "2026-11-12T12:15:00Z")["used_pct"] == 15.0
        router.usage_clear(agent="codex")
        assert shared_path.read_bytes() == before
    finally:
        spool.chmod(mode)

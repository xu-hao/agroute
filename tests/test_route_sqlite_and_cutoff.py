"""SQLite route storage, offline scripts/migrate-routes.py, and TUI display bounds (#83, #86)."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agroute import AgRouteError, Router
from agroute.cli import main
from agroute.route_store import append_route_records
from agroute.routes import _JSON_CACHE, load_repository_routes
from agroute.tui import build_app
from conftest import write_howhard_payload_decision
from textual.widgets import DataTable, Static

MIGRATE_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate-routes.py"


def _route(index=0, decision_id=None):
    return {
        "format": "agroute-route-v1",
        "route_id": f"route-{index:012x}",
        "created_at": f"2026-11-21T00:{index % 60:02d}:00Z",
        "decision_id": decision_id or f"{index:064x}",
        "target_agent": "codex",
        "host_agent": "codex",
        "resolved_target": {"agent": "codex", "model": "gpt-5.4", "effort": "high"},
        "task_text": f"Historical task {index}",
    }


def _store(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state = repo / ".agroute"
    router = Router(project=repo, state_directory=state, include_user_config=False)
    router.usage_set("codex", session_used_pct=5, now="2026-11-21T00:00:00Z")
    return repo, state, router


def _write_jsonl(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _run_migrate(*args):
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    return subprocess.run(
        [sys.executable, str(MIGRATE_SCRIPT), "--no-user-config", *map(str, args)],
        env=env,
        text=True,
        capture_output=True,
    )


def test_runtime_ignores_loose_jsonl_and_decisions_json_without_unlinking(tmp_path):
    repo, state, router = _store(tmp_path)
    _write_jsonl(state / "routes.jsonl", [_route(0)])
    _write_jsonl(state / "plans.jsonl", [_route(1)])
    dec_dir = repo / ".howhard" / "decisions"
    dec_dir.mkdir(parents=True)
    dec_file = dec_dir / f"{0:064x}.json"
    dec_file.write_text(json.dumps({"task_type": "legacy-loose"}), encoding="utf-8")

    # Runtime must have zero awareness of retired JSONL/JSON files and never unlink them.
    assert load_repository_routes(repo, state, "example") == []
    assert router.list_routes(all_repos=False)["routes"] == []
    assert (state / "routes.jsonl").is_file()
    assert (state / "plans.jsonl").is_file()
    assert dec_file.is_file()


def test_migrate_routes_script_preserves_every_line_unlinks_and_is_idempotent(tmp_path):
    repo, state, router = _store(tmp_path)
    records = [
        _route(0),
        {"opaque": True},
        {"opaque": True},
        {
            "format": "agroute-route-close-v1",
            "route_id": _route(0)["route_id"],
            "close_status": "done",
            "closed_at": "2026-11-21T01:00:00Z",
            "credit_usd": 1.25,
        },
    ]
    _write_jsonl(state / "routes.jsonl", records)
    with (state / "routes.jsonl").open("a", encoding="utf-8") as stream:
        stream.write("not valid JSON\n\n")
    original = (state / "routes.jsonl").read_bytes()
    _write_jsonl(state / "plans.jsonl", [_route(1)])

    res = _run_migrate("--directory", repo, "--state-dir", state, "--json")
    assert res.returncode == 0, res.stderr
    summary = json.loads(res.stdout)
    assert summary["plans_files"] == 1
    assert summary["routes_files"] == 1
    assert summary["lines"] == 7

    assert (state / "routes.sqlite3").is_file()
    assert not (state / "routes.jsonl").exists()
    assert not (state / "plans.jsonl").exists()
    shown = router.show_route(_route(0)["route_id"], all_repos=False)
    assert shown["route_status"] == "done"
    assert shown["credit_usd"] == 1.25
    with sqlite3.connect(state / "routes.sqlite3") as db:
        payloads = [row[0] for row in db.execute("SELECT payload FROM route_events ORDER BY sequence")]
    assert len(payloads) == 7

    # Simulate a crash after commit before unlink: re-running migrate-routes.py adds no duplicate rows.
    (state / "routes.jsonl").write_bytes(original)
    res2 = _run_migrate("--directory", repo, "--state-dir", state, "--json")
    assert res2.returncode == 0, res2.stderr
    assert not (state / "routes.jsonl").exists()
    with sqlite3.connect(state / "routes.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM route_events").fetchone()[0] == 7

    router.close_route(_route(1)["route_id"], status="cancelled")
    fresh = Router(project=repo, state_directory=state, include_user_config=False)
    assert fresh.show_route(_route(1)["route_id"], all_repos=False)["route_status"] == "cancelled"


def test_migrate_routes_script_rolls_back_on_failure_and_refuses_unclaimed_or_symlinks(tmp_path):
    repo, state, _ = _store(tmp_path)
    _write_jsonl(state / "routes.jsonl", [_route(0)])
    assert _run_migrate("--directory", repo, "--state-dir", state).returncode == 0

    _write_jsonl(state / "routes.jsonl", [_route(1), _route(2)])
    original = (state / "routes.jsonl").read_bytes()
    with sqlite3.connect(state / "routes.sqlite3") as db:
        db.execute(
            """CREATE TRIGGER reject_second BEFORE INSERT ON route_events
            WHEN CAST(NEW.payload AS TEXT) LIKE '%route-000000000002%'
            BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END"""
        )
    failed = _run_migrate("--directory", repo, "--state-dir", state, "--json")
    assert failed.returncode == 2
    assert "injected migration failure" in failed.stderr
    assert (state / "routes.jsonl").read_bytes() == original
    with sqlite3.connect(state / "routes.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM route_events").fetchone()[0] == 1
        db.execute("DROP TRIGGER reject_second")
    assert _run_migrate("--directory", repo, "--state-dir", state).returncode == 0
    assert len(load_repository_routes(repo, state, "example")) == 3
    assert not (state / "routes.jsonl").exists()

    # Unclaimed store and symlinked input must be refused without unlinking target.
    unclaimed = tmp_path / "unclaimed"
    unclaimed.mkdir()
    _write_jsonl(unclaimed / "routes.jsonl", [_route(9)])
    bad = _run_migrate("--directory", repo, "--state-dir", unclaimed)
    assert bad.returncode == 2
    assert (unclaimed / "routes.jsonl").is_file()

    outside = tmp_path / "outside.jsonl"
    _write_jsonl(outside, [_route(10)])
    (state / "routes.jsonl").symlink_to(outside)
    sym_res = _run_migrate("--directory", repo, "--state-dir", state)
    assert sym_res.returncode == 2
    assert outside.is_file()
    (state / "routes.jsonl").unlink()


def test_readonly_sqlite_store_serves_routes_without_wal_sidecars(tmp_path):
    repo, state, router = _store(tmp_path)
    append_route_records(state, [_route(0), _route(1)])
    files = list(state.iterdir())
    modes = {path: path.stat().st_mode for path in [state, *files]}
    try:
        for path in files:
            path.chmod(0o444)
        state.chmod(0o555)
        rows = load_repository_routes(repo, state, "example")
        assert {row["route_id"] for row in rows} == {_route(0)["route_id"], _route(1)["route_id"]}
    finally:
        for path, mode in modes.items():
            path.chmod(mode)


def test_decision_summary_from_payloads_sqlite_is_small_and_persistent(tmp_path):
    repo, state, router = _store(tmp_path)
    hh = repo / ".howhard"
    dec_id = f"{0:064x}"
    cells = [
        {
            "model": "codex/gpt-5.4",
            "effort": "high",
            "session_cost_usd": 0.2,
            "needed_credit_usd": 2,
            "P_clean": {"mean": 0.9},
        }
        for _ in range(12)
    ]
    cells[-1] = {**cells[-1], "model": "codex/chosen-beyond-top-eight", "session_cost_usd": 0.7}
    document = {
        "chosen": cells[-1],
        "task_type": "migration",
        "risk_class": "contract",
        "context": {"task_text": "Summary task " + "x" * 10000},
        "selection": {"cap_met": False, "needs_review": True, "cells": cells},
        "semantic": {"giant": "x" * 1_500_000},
    }
    write_howhard_payload_decision(hh, dec_id, document)
    append_route_records(state, [_route(0, decision_id=dec_id)])

    first = router.show_route(dec_id, all_repos=False)
    assert len(first["forecasts"]) == 8
    assert first["task_type"] == "migration"
    with sqlite3.connect(state / "routes.sqlite3") as db:
        summary = db.execute("SELECT summary FROM decision_summaries").fetchone()[0]
    assert len(summary) < 20000
    assert "semantic" not in summary
    assert all("semantic" not in str(v) for v in _JSON_CACHE.values())


def test_calibration_reads_sqlite_routes_and_close_metrics(tmp_path):
    from agroute.calibration import _collect_telemetry_sources

    repo, state, router = _store(tmp_path)
    append_route_records(
        state,
        [
            _route(0),
            {
                "format": "agroute-route-close-v1",
                "route_id": _route(0)["route_id"],
                "close_status": "done",
                "closed_at": "2026-11-21T01:00:00Z",
                "credit_usd": 2.5,
            },
        ],
    )
    _, observations, routes = _collect_telemetry_sources(router.config)
    assert routes[0]["route_id"] == _route(0)["route_id"]
    assert routes[0]["close_status"] == "done"
    assert observations[0]["credit_usd"] == 2.5


@pytest.mark.parametrize("flags, expected", [([], 200), (["--limit", "2"], 2), (["--all"], None), (["--limit", "0"], 0)])
def test_tui_cli_passes_display_limit(flags, expected, monkeypatch):
    calls = []
    monkeypatch.setattr("agroute.tui.run", lambda *args, **kwargs: calls.append(kwargs) or 0)
    assert main(["tui", "--no-probe", *flags]) == 0
    assert calls[0]["limit"] == expected


def test_tui_rejects_negative_and_conflicting_limits():
    for flags in (["--limit", "-1"], ["--limit", "2", "--all"]):
        with pytest.raises(SystemExit) as exc:
            main(["tui", *flags])
        assert exc.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("limit, count", [(2, 4), (None, 7), (0, 2)])
async def test_tui_cutoff_preserves_old_active_rows_and_total_counts(limit, count):
    from test_tui import _make_snapshot

    snapshot = _make_snapshot()
    template = snapshot["routes"][0]
    snapshot["routes"] = []
    for index in range(7):
        row = deepcopy(template)
        row.update(
            route_id=f"route-{index}",
            created_at=f"2026-11-08T20:{index:02d}:00Z",
            route_status="routed" if index == 0 else "review" if index == 1 else "done",
            status="routed" if index < 2 else "clean",
            outcome=None if index < 2 else "clean",
        )
        snapshot["routes"].insert(0, row)
    snapshot.update(routed_count=2, observed_count=5)
    app = build_app(lambda: snapshot, limit=limit, refresh_interval=60)
    async with app.run_test(size=(140, 28)) as pilot:
        await pilot.pause()
        table = app.query_one("#routes", DataTable)
        assert table.row_count == count
        assert table.get_row("route-0") and table.get_row("route-1")
        if limit == 2:
            assert table.get_row("route-6") and table.get_row("route-5")
        assert "2 routed" in str(app.query_one("#route-subject", Static).render())
        assert "5 observed" in str(app.query_one("#route-subject", Static).render())
        assert len(snapshot["routes"]) == 7


def test_concurrent_route_closure_preserves_all_events(tmp_path):
    repo, state, router = _store(tmp_path)
    append_route_records(state, [_route(i) for i in range(12)])
    code = """
import sys
from agroute import Router
router = Router(project=sys.argv[1], state_directory=sys.argv[2], include_user_config=False)
router.close_route("route-" + format(int(sys.argv[3]), "012x"), status="cancelled")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    processes = []
    try:
        for index in range(12):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", code, str(repo), str(state), str(index)],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
    with sqlite3.connect(state / "routes.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM route_events").fetchone()[0] == 24
    assert {row["route_status"] for row in router.snapshot(all_repos=False)["routes"]} == {"cancelled"}


def test_unclaimed_sqlite_store_is_refused_and_sidecars_are_known(tmp_path):
    from agroute.state import KNOWN_STORE_ENTRIES, validate_store

    assert {"routes.sqlite3", "routes.sqlite3-wal", "routes.sqlite3-shm", "routes.sqlite3-journal"} <= KNOWN_STORE_ENTRIES
    assert "routes.jsonl" not in KNOWN_STORE_ENTRIES
    assert "plans.jsonl" not in KNOWN_STORE_ENTRIES
    store = tmp_path / "unclaimed"
    store.mkdir()
    sqlite3.connect(store / "routes.sqlite3").close()
    with pytest.raises(AgRouteError, match="unclaimed"):
        validate_store(store)


def test_concurrent_cold_summary_readers_inflate_payloads_sqlite_once(tmp_path):
    repo, state, _ = _store(tmp_path)
    dec_id = f"{0:064x}"
    append_route_records(state, [_route(0, decision_id=dec_id)])
    hh = repo / ".howhard"
    write_howhard_payload_decision(hh, dec_id, {"task_type": "cold", "semantic": {"large": "x" * 1_500_000}})
    marker = tmp_path / "inflations"
    code = """
from pathlib import Path
import sys
import agroute.route_store as rs
from agroute.routes import load_repository_routes
orig = rs._decompress_zstd
def tracked(blob):
    with Path(sys.argv[3]).open("ab") as counter:
        counter.write(b"inflated\\n")
    return orig(blob)
rs._decompress_zstd = tracked
assert load_repository_routes(Path(sys.argv[1]), Path(sys.argv[2]), "example")[0]["task_type"] == "cold"
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    processes = []
    try:
        for _ in range(4):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", code, str(repo), str(state), str(marker)],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
            )
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
    assert marker.read_text(encoding="utf-8").splitlines() == ["inflated"]
    with sqlite3.connect(state / "routes.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM decision_summaries").fetchone()[0] == 1

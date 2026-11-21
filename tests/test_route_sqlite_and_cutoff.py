"""Lossless route storage, persistent receipt summaries, and display bounds (#83)."""

from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from agroute import Router
from agroute.cli import main
from agroute.routes import load_repository_routes
from agroute.tui import build_app
from textual.widgets import DataTable, Static


def _route(index=0):
    return {
        "format": "agroute-route-v1", "route_id": f"route-{index:012x}",
        "created_at": f"2026-11-21T00:{index % 60:02d}:00Z",
        "decision_id": f"decision-{index}", "target_agent": "codex", "host_agent": "codex",
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


def _write(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_jsonl_migration_preserves_every_line_close_lookup_and_is_idempotent(tmp_path):
    repo, state, router = _store(tmp_path)
    records = [_route(0), {"opaque": True}, {"opaque": True}, {"format": "agroute-route-close-v1",
        "route_id": _route(0)["route_id"], "close_status": "done",
        "closed_at": "2026-11-21T01:00:00Z", "credit_usd": 1.25}]
    _write(state / "routes.jsonl", records)
    with (state / "routes.jsonl").open("a") as stream:
        stream.write("not valid JSON\n\n")
    original = (state / "routes.jsonl").read_bytes()
    _write(state / "plans.jsonl", [_route(1)])
    shown = router.show_route(_route(0)["route_id"], all_repos=False)
    assert shown["route_status"] == "done"
    assert shown["credit_usd"] == 1.25
    assert (state / "routes.sqlite3").is_file()
    assert not (state / "routes.jsonl").exists()
    assert not (state / "plans.jsonl").exists()
    with sqlite3.connect(state / "routes.sqlite3") as db:
        payloads = [row[0] for row in db.execute("SELECT payload FROM route_events ORDER BY sequence")]
    assert len(payloads) == 7  # duplicate, malformed, and blank lines survive, too.
    # Simulate a crash after commit but before unlink: importing again must add no rows.
    (state / "routes.jsonl").write_bytes(original)
    assert len(load_repository_routes(repo, state, "example")) == 2
    with sqlite3.connect(state / "routes.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM route_events").fetchone()[0] == 7
    assert not (state / "routes.jsonl").exists()
    # New lifecycle events use SQLite and remain visible across Router instances.
    router.close_route(_route(1)["route_id"], status="cancelled")
    assert not (state / "routes.jsonl").exists()
    fresh = Router(project=repo, state_directory=state, include_user_config=False)
    assert fresh.show_route(_route(1)["route_id"], all_repos=False)["route_status"] == "cancelled"


def test_readonly_store_merges_unmigrated_jsonl_without_losing_sqlite_rows(tmp_path):
    repo, state, router = _store(tmp_path)
    _write(state / "routes.jsonl", [_route(0)])
    router.snapshot(all_repos=False)
    assert (state / "routes.sqlite3").exists()
    _write(state / "routes.jsonl", [_route(1)])
    _write(state / "plans.jsonl", [_route(2)])
    files = list(state.iterdir())
    modes = {path: path.stat().st_mode for path in [state, *files]}
    try:
        for path in files:
            path.chmod(0o444)
        state.chmod(0o555)
        rows = load_repository_routes(repo, state, "example")
        assert {row["route_id"] for row in rows} == {_route(i)["route_id"] for i in range(3)}
        assert (state / "routes.jsonl").exists()
        assert (state / "plans.jsonl").exists()
    finally:
        for path, mode in modes.items():
            path.chmod(mode)


def test_decision_summary_is_small_persistent_and_invalidated_by_file_signature(tmp_path, monkeypatch):
    repo, state, router = _store(tmp_path)
    hh = repo / ".howhard" / "decisions"
    hh.mkdir(parents=True)
    decision = hh / "decision-0.json"
    cells = [{"model": "codex/gpt-5.4", "effort": "high", "session_cost_usd": 0.2,
              "needed_credit_usd": 2, "P_clean": {"mean": .9}} for _ in range(12)]
    cells[-1] = {**cells[-1], "model": "codex/chosen-beyond-top-eight", "session_cost_usd": 0.7}
    document = {"chosen": cells[-1], "task_type": "migration", "risk_class": "contract",
        "context": {"task_text": "Summary task " + "x" * 10000},
        "selection": {"cap_met": False, "needs_review": True, "cells": cells},
        "semantic": {"giant": "x" * 6_200_000}}
    decision.write_text(json.dumps(document))
    _write(state / "routes.jsonl", [_route(0)])
    first = router.show_route("decision-0", all_repos=False)
    assert len(first["forecasts"]) == 8
    assert first["task_type"] == "migration"
    # A fresh interpreter must use the durable summary, without opening the receipt.
    code = '''
import json, sys
from pathlib import Path
from agroute.routes import load_repository_routes, _JSON_CACHE
original = Path.open
def guarded(self, *args, **kwargs):
    if str(self) == sys.argv[3]:
        raise AssertionError("receipt reparsed in a new process")
    return original(self, *args, **kwargs)
Path.open = guarded
rows = load_repository_routes(Path(sys.argv[1]), Path(sys.argv[2]), "example")
assert rows[0]["task_type"] == "migration"
assert all("semantic" not in str(v) for v in _JSON_CACHE.values())
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    subprocess.run([sys.executable, "-c", code, str(repo), str(state), str(decision)], env=env, check=True)
    with sqlite3.connect(state / "routes.sqlite3") as db:
        summary = db.execute("SELECT summary FROM decision_summaries").fetchone()[0]
    assert len(summary) < 20000
    assert "semantic" not in summary
    from agroute.routes import _JSON_CACHE
    assert str(decision) not in _JSON_CACHE
    document["task_type"] = "changed"
    decision.write_text(json.dumps(document))
    assert router.show_route("decision-0", all_repos=False)["task_type"] == "changed"


def test_calibration_reads_all_migrated_routes_and_close_metrics(tmp_path):
    from agroute.calibration import _collect_telemetry_sources
    repo, state, router = _store(tmp_path)
    _write(state / "routes.jsonl", [_route(0), {"format": "agroute-route-close-v1",
        "route_id": _route(0)["route_id"], "close_status": "done",
        "closed_at": "2026-11-21T01:00:00Z", "credit_usd": 2.5}])
    router.snapshot(all_repos=False)
    assert not (state / "routes.jsonl").exists()
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
        row.update(route_id=f"route-{index}", created_at=f"2026-11-08T20:{index:02d}:00Z",
                   route_status="routed" if index == 0 else "review" if index == 1 else "done",
                   status="routed" if index < 2 else "clean", outcome=None if index < 2 else "clean")
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


def test_concurrent_migration_and_route_closure_preserve_all_events(tmp_path):
    repo, state, router = _store(tmp_path)
    _write(state / "routes.jsonl", [_route(i) for i in range(12)])
    code = '''
import sys
from agroute import Router
router = Router(project=sys.argv[1], state_directory=sys.argv[2], include_user_config=False)
router.close_route("route-" + format(int(sys.argv[3]), "012x"), status="cancelled")
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    processes = []
    try:
        for index in range(12):
            processes.append(subprocess.Popen([sys.executable, "-c", code, str(repo), str(state), str(index)],
                                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
    assert not (state / "routes.jsonl").exists()
    with sqlite3.connect(state / "routes.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM route_events").fetchone()[0] == 24
    assert {row["route_status"] for row in router.snapshot(all_repos=False)["routes"]} == {"cancelled"}


def test_unclaimed_sqlite_store_is_refused_and_sidecars_are_known(tmp_path):
    from agroute.state import KNOWN_STORE_ENTRIES, validate_store
    from agroute import AgRouteError
    assert {"routes.sqlite3", "routes.sqlite3-wal", "routes.sqlite3-shm", "routes.sqlite3-journal"} <= KNOWN_STORE_ENTRIES
    store = tmp_path / "unclaimed"
    store.mkdir()
    sqlite3.connect(store / "routes.sqlite3").close()
    with pytest.raises(AgRouteError, match="unclaimed"):
        validate_store(store)


def test_concurrent_cold_summary_readers_parse_receipt_once(tmp_path):
    repo, state, _ = _store(tmp_path)
    _write(state / "routes.jsonl", [_route(0)])
    hh = repo / ".howhard" / "decisions"
    hh.mkdir(parents=True)
    decision = hh / "decision-0.json"
    decision.write_text(json.dumps({"task_type": "cold", "semantic": {"large": "x" * 6_200_000}}))
    marker = tmp_path / "parses"
    code = '''
from pathlib import Path
import sys
from agroute.routes import load_repository_routes
original = Path.open
def tracked(self, *args, **kwargs):
    if str(self) == sys.argv[3]:
        with original(Path(sys.argv[4]), "ab") as counter:
            counter.write(b"parsed\\n")
    return original(self, *args, **kwargs)
Path.open = tracked
assert load_repository_routes(Path(sys.argv[1]), Path(sys.argv[2]), "example")[0]["task_type"] == "cold"
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    processes = []
    try:
        for _ in range(4):
            processes.append(subprocess.Popen([sys.executable, "-c", code, str(repo), str(state),
                                               str(decision), str(marker)], env=env,
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        for process in processes:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, (stdout, stderr)
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()
    assert marker.read_text().splitlines() == ["parsed"]
    with sqlite3.connect(state / "routes.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM decision_summaries").fetchone()[0] == 1


def test_migration_rolls_back_on_insert_failure_and_preserves_source(tmp_path):
    from agroute import AgRouteError
    repo, state, _ = _store(tmp_path)
    _write(state / "routes.jsonl", [_route(0)])
    load_repository_routes(repo, state, "example")
    _write(state / "routes.jsonl", [_route(1), _route(2)])
    original = (state / "routes.jsonl").read_bytes()
    with sqlite3.connect(state / "routes.sqlite3") as db:
        db.execute("""CREATE TRIGGER reject_second BEFORE INSERT ON route_events
            WHEN CAST(NEW.payload AS TEXT) LIKE '%route-000000000002%'
            BEGIN SELECT RAISE(ABORT, 'injected migration failure'); END""")
    with pytest.raises(AgRouteError, match="injected migration failure"):
        load_repository_routes(repo, state, "example")
    assert (state / "routes.jsonl").read_bytes() == original
    with sqlite3.connect(state / "routes.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM route_events").fetchone()[0] == 1
        db.execute("DROP TRIGGER reject_second")
    assert len(load_repository_routes(repo, state, "example")) == 3
    assert not (state / "routes.jsonl").exists()

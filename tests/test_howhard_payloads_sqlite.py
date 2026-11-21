"""Read howhard 4.0 payloads.sqlite3 receipts, summaries, and decision IDs (#86)."""

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
import zstandard as zstd

from agroute import Router
from agroute.cli import main
from agroute.routes import _JSON_CACHE, load_repository_routes


def _canonical(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value: dict) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _init_payloads_db(hh_root: Path) -> Path:
    hh_root.mkdir(parents=True, exist_ok=True)
    db_path = hh_root / "payloads.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, payload BLOB NOT NULL)")
        db.execute(
            """CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY,
                payload BLOB NOT NULL,
                created_at TEXT NOT NULL,
                chosen TEXT,
                context_summary TEXT,
                selection TEXT,
                task_type TEXT,
                risk_class TEXT,
                cap_met INTEGER,
                needs_review INTEGER
            )"""
        )
    return db_path


def _insert_howhard_v9_decision(
    hh_root: Path,
    *,
    model: str = "codex/gpt-5.4",
    effort: str = "high",
    task_text: str = "Wire payloads.sqlite3 reader",
    task_type: str = "task",
    risk_class: str = "standard",
    cap_met: bool = True,
    needs_review: bool = False,
    created_at: str = "2026-11-21T06:00:00+00:00",
    giant_bytes: int = 200_000,
) -> tuple[str, dict]:
    cells = [
        {
            "model": f"codex/model-{idx}",
            "family": f"fam-{idx}",
            "effort": "medium",
            "session_cost_usd": 0.1 + idx * 0.01,
            "needed_credit_usd": 1.0 + idx * 0.1,
            "cost_per_credit": 0.1,
            "J": 20.0 + idx,
            "cap_met": cap_met,
            "needs_review": needs_review,
            "P_clean": {"mean": 0.9},
            "P_loud": {"mean": 0.08},
            "P_silent": {"mean": 0.02},
        }
        for idx in range(10)
    ]
    chosen_cell = {
        "model": model,
        "family": "fam-chosen",
        "effort": effort,
        "session_cost_usd": 0.42,
        "needed_credit_usd": 4.2,
        "cost_per_credit": 0.1,
        "credit_budget": {"available_credit_usd": 20.0},
        "J": 15.5,
        "cap_met": cap_met,
        "needs_review": needs_review,
        "P_clean": {"mean": 0.93},
        "P_loud": {"mean": 0.05},
        "P_silent": {"mean": 0.02},
    }
    cells.append(chosen_cell)
    doc = {
        "format": "howhard-decision-v9",
        "project": "local",
        "project_identity": "a" * 32,
        "task_type": task_type,
        "risk_class": risk_class,
        "chosen": {"model": model, "family": "fam-chosen", "effort": effort},
        "seed": 42,
        "context": {
            "encoder": "howhard-context-v1",
            "task_text": task_text,
            "project_context": {"fingerprint": "b" * 64},
        },
        "selection": {
            "objective": "credit-saving",
            "cap_met": cap_met,
            "needs_review": needs_review,
            "cells": cells,
        },
        "semantic": {"giant": "s" * giant_bytes},
    }
    dec_id = _digest(doc)
    raw = _canonical(doc) + b"\n"
    compressed = zstd.ZstdCompressor(level=3).compress(raw)
    db_path = _init_payloads_db(hh_root)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO decisions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                dec_id,
                compressed,
                created_at,
                json.dumps(doc["chosen"], sort_keys=True, separators=(",", ":")),
                json.dumps(
                    {"task_text": task_text, "project_fingerprint": "b" * 64},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                json.dumps({"objective": "credit-saving"}, sort_keys=True, separators=(",", ":")),
                task_type,
                risk_class,
                int(cap_met),
                int(needs_review),
            ),
        )
    return dec_id, doc


def _setup_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "agroute.toml").write_text(
        """
[routing]
default_host = "codex"
[agents.codex]
native_candidates = [{ model = "gpt-5.4", effort = "high" }]
cli_candidates = [{ model = "gpt-5.4", effort = "high" }]
[agents.claude]
enabled = false
[agents.agy]
enabled = false
""",
        encoding="utf-8",
    )
    state = repo / ".agroute"
    router = Router(project=repo, state_directory=state, include_user_config=False)
    router.usage_set("codex", session_used_pct=10.0, weekly_used_pct=10.0, now="2026-11-21T06:00:00Z")
    return repo, state, router


def test_payloads_sqlite_summary_cached_once_without_retaining_semantic(tmp_path: Path):
    repo, state, router = _setup_repo(tmp_path)
    hh_root = repo / ".howhard"
    dec_id, _ = _insert_howhard_v9_decision(
        hh_root,
        task_text="Migrate payloads to SQLite\n" + ("detail " * 800),
        task_type="migration",
        risk_class="contract",
        cap_met=False,
        needs_review=True,
        giant_bytes=1_500_000,
    )
    assert not (hh_root / "decisions").exists()

    plan = router.plan(
        "codex",
        {"decision_id": dec_id, "chosen": {"model": "codex/gpt-5.4", "effort": "high"}},
        prompt="Run migration",
        now="2026-11-21T06:01:00Z",
    )
    shown = router.show_route(plan["route_id"], all_repos=False)
    assert shown["task_type"] == "migration"
    assert shown["risk_class"] == "contract"
    assert shown["label"] == "Migrate payloads to SQLite"
    assert len(shown["forecasts"]) == 8
    assert all("semantic" not in str(v) for v in _JSON_CACHE.values())

    # Second process must hit routes.sqlite3 decision_summaries without opening payloads.sqlite3.
    payloads_db = hh_root / "payloads.sqlite3"
    payloads_db.unlink()
    code = """
import sys
from pathlib import Path
from agroute.routes import load_repository_routes
rows = load_repository_routes(Path(sys.argv[1]), Path(sys.argv[2]), "repo")
assert len(rows) == 1
assert rows[0]["task_type"] == "migration"
assert len(rows[0]["forecasts"]) == 8
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
    subprocess.run([sys.executable, "-c", code, str(repo), str(state)], env=env, check=True)


def test_direct_howhard_decisions_in_payloads_sqlite_surface_without_events_jsonl(tmp_path: Path):
    repo, state, router = _setup_repo(tmp_path)
    hh_root = repo / ".howhard"
    dec_id, _ = _insert_howhard_v9_decision(
        hh_root,
        model="codex/gpt-5.4",
        effort="high",
        task_text="Direct howhard 4.0 route",
        cap_met=False,
        needs_review=True,
    )
    rows = load_repository_routes(repo, state, "repo")
    assert len(rows) == 1
    row = rows[0]
    assert row["route_id"] == f"hh-{dec_id[:12]}"
    assert row["decision_id"] == dec_id
    assert row["route_status"] == "review"
    assert row["needs_review"] is True
    assert row["cap_met"] is False
    assert row["session_cost_usd"] == pytest.approx(0.42)
    assert row["needed_credit_usd"] == pytest.approx(4.2)
    assert row["label"] == "Direct howhard 4.0 route"

    (hh_root / "outcomes.jsonl").write_text(
        json.dumps(
            {
                "id": "exec-hh4-1",
                "decision_id": dec_id,
                "model": "codex/gpt-5.4",
                "effort": "high",
                "outcome": "clean",
                "cause": None,
                "tokens": 12000,
                "seconds": 45.0,
                "cost_usd": None,
                "note": "completed",
                "amends": None,
                "observed_at": "2026-11-21T06:05:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    updated = router.show_route(dec_id, all_repos=False)
    assert updated["route_status"] == "done"
    assert updated["outcome"] == "clean"
    assert updated["execution_id"] == "exec-hh4-1"


def test_column_fallback_upgrades_when_zstd_restored_and_refuses_symlink(tmp_path: Path, monkeypatch):
    import agroute.route_store as route_store

    repo, state, router = _setup_repo(tmp_path)
    hh_root = repo / ".howhard"
    dec_id, _ = _insert_howhard_v9_decision(
        hh_root,
        task_text="Fallback column summary",
        task_type="audit",
        risk_class="critical",
        cap_met=True,
        needs_review=True,
    )
    monkeypatch.setattr(route_store, "_decompress_zstd", lambda _blob: None)
    rows = load_repository_routes(repo, state, "repo")
    assert len(rows) == 1
    assert rows[0]["task_type"] == "audit"
    assert rows[0]["risk_class"] == "critical"
    assert rows[0]["needs_review"] is True
    assert rows[0]["label"] == "Fallback column summary"
    assert rows[0]["forecasts"] == []

    monkeypatch.undo()
    upgraded = load_repository_routes(repo, state, "repo")
    assert len(upgraded[0]["forecasts"]) == 8

    # Symlinked payloads.sqlite3 must not be followed.
    repo2, state2, _ = _setup_repo(tmp_path / "sym")
    hh2 = repo2 / ".howhard"
    hh2.mkdir()
    (hh2 / "payloads.sqlite3").symlink_to(hh_root / "payloads.sqlite3")
    assert load_repository_routes(repo2, state2, "repo2") == []


def test_agr_plan_cli_accepts_decision_id_from_payloads_sqlite(tmp_path: Path, capsys, monkeypatch):
    repo, state, _ = _setup_repo(tmp_path)
    monkeypatch.chdir(repo)
    hh_root = repo / ".howhard"
    dec_id, _ = _insert_howhard_v9_decision(hh_root, model="codex/gpt-5.4", effort="high")
    rc = main(
        [
            "--config",
            str(repo / "agroute.toml"),
            "--state-dir",
            str(state),
            "--no-user-config",
            "plan",
            "--host",
            "codex",
            "--decision",
            dec_id,
            "--prompt",
            "Execute from payloads.sqlite3 receipt ID",
            "--now",
            "2026-11-21T06:02:00Z",
            "--json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision_id"] == dec_id
    assert out["execution_mode"] == "native"
    assert out["needed_credit_usd"] == pytest.approx(4.2)
    assert out["session_cost_usd"] == pytest.approx(0.42)

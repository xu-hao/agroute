"""Lossless SQLite route events and cross-process howhard 4.0 decision-summary caching.

SQLite's immediate transactions serialize writes and cache misses. The default
rollback journal also permits read-only access without writable WAL sidecars.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3

from .validation import AgRouteError

DATABASE = "routes.sqlite3"
HOWHARD_PAYLOADS_DB = "payloads.sqlite3"
_HEX64 = frozenset("0123456789abcdef")


def writable_store(root: Path) -> bool:
    parent = root
    while not parent.exists() and parent.parent != parent:
        parent = parent.parent
    if not os.access(parent, os.W_OK):
        return False
    db = root / DATABASE
    return not db.exists() or os.access(db, os.W_OK)


@contextmanager
def _connect(root: Path, *, writable: bool):
    connection = None
    try:
        if writable:
            root.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(root / DATABASE, timeout=30, isolation_level=None)
        elif (root / DATABASE).exists():
            connection = sqlite3.connect(
                (root / DATABASE).resolve().as_uri() + "?mode=ro",
                uri=True,
                timeout=30,
                isolation_level=None,
            )
        if connection is not None:
            connection.execute("PRAGMA busy_timeout = 30000")
            if writable:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS route_events (
                        sequence INTEGER PRIMARY KEY,
                        payload BLOB NOT NULL,
                        source TEXT,
                        line_number INTEGER,
                        digest TEXT,
                        UNIQUE(source, line_number, digest)
                    )"""
                    )
                    connection.execute(
                        """CREATE TABLE IF NOT EXISTS decision_summaries (
                        decision_id TEXT NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        size INTEGER NOT NULL,
                        summary TEXT NOT NULL,
                        PRIMARY KEY(decision_id, mtime_ns, size)
                    )"""
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        yield connection
    except sqlite3.Error as exc:
        raise AgRouteError(f"route store {root / DATABASE}: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()


def _decode(payload):
    try:
        record = json.loads(payload)
    except (ValueError, UnicodeError):
        return None
    return record if isinstance(record, dict) else None


def read_route_records(root: Path) -> list[dict]:
    """Read all route and close events from `<state_root>/routes.sqlite3`."""
    db_path = root / DATABASE
    if db_path.is_symlink() or not db_path.is_file():
        return []
    with _connect(root, writable=False) as db:
        if db is None:
            return []
        rows: list[dict] = []
        for (payload,) in db.execute("SELECT payload FROM route_events ORDER BY sequence"):
            record = _decode(payload)
            if record is not None:
                rows.append(record)
        return rows


def append_route_records(root: Path, records: list[dict]) -> None:
    if (root / DATABASE).is_symlink():
        raise AgRouteError(f"route store: refusing symlink at {root / DATABASE}")
    if not writable_store(root):
        raise AgRouteError(f"route store is read-only: {root}")
    with _connect(root, writable=True) as db:
        db.execute("BEGIN IMMEDIATE")
        try:
            db.executemany(
                "INSERT INTO route_events(payload) VALUES (?)",
                [(json.dumps(record, ensure_ascii=False).encode("utf-8"),) for record in records],
            )
            db.commit()
        except BaseException:
            db.rollback()
            raise


def _decompress_zstd(blob: bytes) -> bytes | None:
    try:
        from compression import zstd as std_zstd  # type: ignore[import-not-found]

        return std_zstd.decompress(blob)
    except ImportError:
        pass
    except Exception:
        return None
    try:
        import zstandard

        return zstandard.ZstdDecompressor().decompress(blob)
    except ImportError:
        return None
    except Exception:
        return None


def _connect_howhard_payloads_ro(hh_root: Path | None):
    if hh_root is None:
        return None
    path = hh_root / HOWHARD_PAYLOADS_DB
    if path.is_symlink() or not path.is_file():
        return None
    try:
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        db.execute("PRAGMA busy_timeout = 30000")
        return db
    except sqlite3.Error:
        return None


def _summary_from_howhard_columns(
    chosen_raw,
    context_raw,
    task_type,
    risk_class,
    cap_met,
    needs_review,
) -> dict:
    chosen: dict = {}
    if isinstance(chosen_raw, str):
        try:
            parsed = json.loads(chosen_raw)
            if isinstance(parsed, dict):
                chosen = {k: parsed[k] for k in ("model", "family", "effort") if isinstance(parsed.get(k), str)}
        except ValueError:
            pass
    task_text = None
    if isinstance(context_raw, str):
        try:
            ctx = json.loads(context_raw)
            if isinstance(ctx, dict) and isinstance(ctx.get("task_text"), str):
                task_text = ctx["task_text"][:4000]
        except ValueError:
            pass
    return {
        "task_type": task_type if isinstance(task_type, str) else None,
        "risk_class": risk_class if isinstance(risk_class, str) else None,
        "cap_met": bool(cap_met) if isinstance(cap_met, int) else None,
        "needs_review": bool(needs_review) if isinstance(needs_review, int) else False,
        "chosen": chosen,
        "context": {"task_text": task_text},
        "cells": [],
        "forecasts": [],
    }


def howhard_payload_decisions(hh_root: Path | None) -> list[dict]:
    """List indexed decision rows from `<hh_root>/payloads.sqlite3` without inflating blobs."""
    db = _connect_howhard_payloads_ro(hh_root)
    if db is None:
        return []
    try:
        rows = db.execute(
            "SELECT decision_id, created_at, task_type FROM decisions ORDER BY created_at DESC, decision_id DESC"
        ).fetchall()
        return [
            {
                "decision_id": str(dec_id),
                "at_utc": str(created_at) if created_at is not None else None,
                "task_type": str(task_type) if isinstance(task_type, str) else None,
            }
            for dec_id, created_at, task_type in rows
            if isinstance(dec_id, str) and dec_id
        ]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def load_howhard_decision_document(hh_root: Path | None, decision_id: str) -> dict | None:
    """Inflate one canonical receipt JSON dict from `<hh_root>/payloads.sqlite3` by 64-hex ID."""
    if len(decision_id) != 64 or not all(ch in _HEX64 for ch in decision_id):
        return None
    db = _connect_howhard_payloads_ro(hh_root)
    if db is None:
        return None
    try:
        row = db.execute("SELECT payload FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()
    if row is None or not isinstance(row[0], (bytes, bytearray, memoryview)):
        return None
    raw = _decompress_zstd(bytes(row[0]))
    if raw is None:
        return None
    doc = _decode(raw)
    if doc is not None:
        doc.setdefault("decision_id", decision_id)
    return doc


def decision_summary(root: Path, decision_id: str, hh_root: Path | None, extract) -> dict | None:
    """Decompress a `howhard` 4.0 receipt at most once across writable-store processes."""
    if not decision_id:
        return None
    writable = writable_store(root)
    with _connect(root, writable=writable) as db:
        if db is not None and writable:
            db.execute("BEGIN IMMEDIATE")
        try:
            cached = (
                db.execute(
                    "SELECT summary, size FROM decision_summaries WHERE decision_id=? AND mtime_ns=-1 ORDER BY size DESC LIMIT 1",
                    (decision_id,),
                ).fetchone()
                if db is not None
                else None
            )
            if cached is not None and cached[1] > 0:
                return json.loads(cached[0])

            hh_db = _connect_howhard_payloads_ro(hh_root)
            if hh_db is None:
                return json.loads(cached[0]) if cached is not None else None
            try:
                row = hh_db.execute(
                    "SELECT payload, chosen, context_summary, task_type, risk_class, cap_met, needs_review "
                    "FROM decisions WHERE decision_id=?",
                    (decision_id,),
                ).fetchone()
            except sqlite3.Error:
                row = None
            finally:
                hh_db.close()

            if row is None:
                return json.loads(cached[0]) if cached is not None else None

            blob = bytes(row[0]) if isinstance(row[0], (bytes, bytearray, memoryview)) else b""
            raw = _decompress_zstd(blob) if blob else None
            document = _decode(raw) if raw is not None else None
            if document is not None:
                summary = extract(document)
                del document, raw
                size_marker = max(1, len(blob))
                if db is not None and writable:
                    db.execute("DELETE FROM decision_summaries WHERE decision_id=? AND mtime_ns=-1", (decision_id,))
                    db.execute(
                        "INSERT OR REPLACE INTO decision_summaries VALUES (?, -1, ?, ?)",
                        (decision_id, size_marker, json.dumps(summary)),
                    )
                return summary

            if cached is not None:
                return json.loads(cached[0])

            summary = _summary_from_howhard_columns(*row[1:])
            if db is not None and writable:
                db.execute(
                    "INSERT OR REPLACE INTO decision_summaries VALUES (?, -1, 0, ?)",
                    (decision_id, json.dumps(summary)),
                )
            return summary
        finally:
            if db is not None and writable:
                db.commit()

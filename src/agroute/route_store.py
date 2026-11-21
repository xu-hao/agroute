"""Lossless SQLite route events and cross-process decision-summary caching.

SQLite's immediate transactions serialize migration and cache misses. The default
rollback journal also permits read-only access without writable WAL sidecars.
"""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from .validation import AgRouteError

DATABASE = "routes.sqlite3"
LEGACY_FILES = ("plans.jsonl", "routes.jsonl")


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
            connection = sqlite3.connect((root / DATABASE).resolve().as_uri() + "?mode=ro",
                                         uri=True, timeout=30, isolation_level=None)
        if connection is not None:
            connection.execute("PRAGMA busy_timeout = 30000")
            if writable:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute("""CREATE TABLE IF NOT EXISTS route_events (
                        sequence INTEGER PRIMARY KEY,
                        payload BLOB NOT NULL,
                        source TEXT,
                        line_number INTEGER,
                        digest TEXT,
                        UNIQUE(source, line_number, digest)
                    )""")
                    connection.execute("""CREATE TABLE IF NOT EXISTS decision_summaries (
                        decision_id TEXT NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        size INTEGER NOT NULL,
                        summary TEXT NOT NULL,
                        PRIMARY KEY(decision_id, mtime_ns, size)
                    )""")
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


def _legacy_lines(root: Path):
    for name in LEGACY_FILES:
        path = root / name
        try:
            with path.open("rb") as stream:
                signature = os.fstat(stream.fileno())
                lines = stream.readlines()
        except FileNotFoundError:
            continue
        yield path, signature, lines


def _line_key(source: str, index: int, payload: bytes):
    return source, index, hashlib.sha256(payload).hexdigest()


def _signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size


def _migrate(db, root: Path):
    """Commit and verify every byte before unlinking any legacy input."""
    db.execute("BEGIN IMMEDIATE")
    imported = []
    try:
        for path, signature, lines in _legacy_lines(root):
            for index, payload in enumerate(lines):
                key = _line_key(path.name, index, payload)
                db.execute("INSERT OR IGNORE INTO route_events(payload, source, line_number, digest) VALUES (?, ?, ?, ?)",
                           (payload, *key))
                stored = db.execute("SELECT payload FROM route_events WHERE source=? AND line_number=? AND digest=?",
                                    key).fetchone()
                if stored is None or stored[0] != payload:
                    raise AgRouteError(f"route migration failed to preserve {path}:{index + 1}")
            try:
                unchanged = _signature(path.stat()) == _signature(signature)
            except FileNotFoundError:
                # Another importer may have committed and unlinked this same snapshot.
                unchanged = True
            if not unchanged:
                raise AgRouteError(f"route migration input changed: {path}; retry")
            imported.append((path, signature))
        db.commit()
    except BaseException:
        db.rollback()
        raise
    # A crash here is safe: source/line/digest uniqueness makes a retry idempotent.
    for path, signature in imported:
        try:
            if _signature(path.stat()) == _signature(signature):
                path.unlink()
        except FileNotFoundError:
            pass
        except PermissionError:
            # A permission change after commit leaves a mergeable, deduplicated input.
            pass


def _decode(payload):
    try:
        record = json.loads(payload)
    except (ValueError, UnicodeError):
        return None
    return record if isinstance(record, dict) else None


def read_route_records(root: Path) -> list[dict]:
    """Read all events; migrate writable legacy stores or merge read-only inputs."""
    writable = writable_store(root)
    if not (root / DATABASE).exists() and not any((root / name).exists() for name in LEGACY_FILES):
        return []
    with _connect(root, writable=writable) as db:
        if db is not None and writable:
            _migrate(db, root)
        rows = []
        imported = set()
        if db is not None:
            for payload, source, index, digest in db.execute(
                "SELECT payload, source, line_number, digest FROM route_events ORDER BY sequence"
            ):
                record = _decode(payload)
                if record is not None:
                    rows.append(record)
                if source is not None:
                    imported.add((source, index, digest))
        for path, _, lines in _legacy_lines(root):
            for index, payload in enumerate(lines):
                if _line_key(path.name, index, payload) not in imported:
                    record = _decode(payload)
                    if record is not None:
                        rows.append(record)
        return rows


def append_route_records(root: Path, records: list[dict]) -> None:
    if not writable_store(root):
        raise AgRouteError(f"route store is read-only: {root}")
    with _connect(root, writable=True) as db:
        _migrate(db, root)
        db.execute("BEGIN IMMEDIATE")
        try:
            db.executemany("INSERT INTO route_events(payload) VALUES (?)",
                           [(json.dumps(record, ensure_ascii=False).encode("utf-8"),) for record in records])
            db.commit()
        except BaseException:
            db.rollback()
            raise


def decision_summary(root: Path, decision_id: str, path: Path, extract) -> dict | None:
    """Parse a receipt at most once per signature across writable-store processes."""
    if not path.is_file():
        return None
    writable = writable_store(root)
    with _connect(root, writable=writable) as db:
        for _ in range(3):
            if db is not None and writable:
                # Hold the lock through extraction so concurrent cold readers do not reparse.
                db.execute("BEGIN IMMEDIATE")
            try:
                try:
                    stat = path.stat()
                except OSError:
                    return None
                key = decision_id, stat.st_mtime_ns, stat.st_size
                cached = db.execute(
                    "SELECT summary FROM decision_summaries WHERE decision_id=? AND mtime_ns=? AND size=?", key
                ).fetchone() if db is not None else None
                if cached is not None:
                    return json.loads(cached[0])
                try:
                    document = json.loads(path.read_bytes())
                    current_stat = path.stat()
                except (OSError, ValueError, UnicodeError):
                    return None
                if _signature(current_stat) != _signature(stat):
                    del document
                    continue
                if not isinstance(document, dict):
                    return None
                summary = extract(document)
                del document
                if db is not None and writable:
                    db.execute("INSERT OR REPLACE INTO decision_summaries VALUES (?, ?, ?, ?)",
                               (*key, json.dumps(summary)))
                return summary
            finally:
                if db is not None and writable:
                    db.commit()
    return None

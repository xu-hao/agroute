#!/usr/bin/env python3
"""Standalone one-time offline migration of pre-0.6 plans.jsonl and routes.jsonl into routes.sqlite3."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

from agroute.config import load_config
from agroute.routes import _read_cached_json, _spool_dir
from agroute.state import check
from agroute.usage import _usage_locked
from agroute.validation import AgRouteError

DATABASE = "routes.sqlite3"
LEGACY_FILES = ("plans.jsonl", "routes.jsonl")


def parser():
    root = argparse.ArgumentParser(
        prog="migrate-routes.py",
        description="Migrate pre-0.6 loose plans.jsonl and routes.jsonl into routes.sqlite3.",
    )
    root.add_argument("--config", metavar="PATH", help="optional agroute.toml configuration file")
    root.add_argument(
        "--directory",
        metavar="PATH",
        default=Path.cwd(),
        type=Path,
        help="project directory (default: current working directory)",
    )
    root.add_argument("--state-dir", metavar="PATH", help="explicit durable state directory")
    root.add_argument("--no-user-config", action="store_true", help="ignore user-level config.toml")
    root.add_argument(
        "--all-registered",
        action="store_true",
        help="migrate all repositories registered in the machine-local spool",
    )
    root.add_argument("--json", action="store_true", help="emit JSON migration summary")
    return root


def _signature(stat):
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size


def _line_key(source: str, index: int, payload: bytes):
    return source, index, hashlib.sha256(payload).hexdigest()


def _check_claimed(config):
    check(config)
    store_json = config.state_root / "store.json"
    if store_json.is_symlink() or not store_json.is_file():
        raise AgRouteError(f"state: refusing unclaimed store at {config.state_root}")
    if (config.state_root / DATABASE).is_symlink():
        raise AgRouteError(f"route store: refusing symlink at {config.state_root / DATABASE}")
    for name in LEGACY_FILES:
        cand = config.state_root / name
        if cand.is_symlink():
            raise AgRouteError(f"route migration: refusing symlink at {cand}")


def migrate_store(config) -> dict:
    _check_claimed(config)
    counts = {name: 0 for name in LEGACY_FILES}
    migrated_lines = 0
    raw_bytes = 0

    with _usage_locked(config):
        _check_claimed(config)
        legacy_inputs = []
        for name in LEGACY_FILES:
            path = config.state_root / name
            if path.is_symlink():
                raise AgRouteError(f"route migration: refusing symlink at {path}")
            if not path.exists():
                continue
            if not path.is_file():
                raise AgRouteError(f"route migration: refusing non-regular file {path}")
            with path.open("rb") as stream:
                sig = os.fstat(stream.fileno())
                lines = stream.readlines()
            legacy_inputs.append((path, sig, lines))

        if not legacy_inputs:
            return {
                "store": str(config.state_root),
                "plans_files": 0,
                "routes_files": 0,
                "lines": 0,
                "raw_bytes": 0,
            }

        db = sqlite3.connect(config.state_root / DATABASE, timeout=30, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout = 30000")
            db.execute("BEGIN IMMEDIATE")
            try:
                db.execute(
                    """CREATE TABLE IF NOT EXISTS route_events (
                    sequence INTEGER PRIMARY KEY,
                    payload BLOB NOT NULL,
                    source TEXT,
                    line_number INTEGER,
                    digest TEXT,
                    UNIQUE(source, line_number, digest)
                )"""
                )
                db.execute(
                    """CREATE TABLE IF NOT EXISTS decision_summaries (
                    decision_id TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    summary TEXT NOT NULL,
                    PRIMARY KEY(decision_id, mtime_ns, size)
                )"""
                )
                for path, sig, lines in legacy_inputs:
                    for index, payload in enumerate(lines):
                        key = _line_key(path.name, index, payload)
                        db.execute(
                            "INSERT OR IGNORE INTO route_events(payload, source, line_number, digest) VALUES (?, ?, ?, ?)",
                            (payload, *key),
                        )
                        stored = db.execute(
                            "SELECT payload FROM route_events WHERE source=? AND line_number=? AND digest=?",
                            key,
                        ).fetchone()
                        if stored is None or stored[0] != payload:
                            raise AgRouteError(f"route migration: round-trip mismatch at {path}:{index + 1}")
                    if _signature(path.stat()) != _signature(sig):
                        raise AgRouteError(f"route migration: input changed during import: {path}")
                db.commit()
            except BaseException:
                db.rollback()
                raise

            # Post-commit verification before unlinking any source file.
            for path, sig, lines in legacy_inputs:
                if _signature(path.stat()) != _signature(sig):
                    raise AgRouteError(f"route migration: post-commit input changed: {path}")
                for index, payload in enumerate(lines):
                    key = _line_key(path.name, index, payload)
                    stored = db.execute(
                        "SELECT payload FROM route_events WHERE source=? AND line_number=? AND digest=?",
                        key,
                    ).fetchone()
                    if stored is None or stored[0] != payload:
                        raise AgRouteError(f"route migration: post-commit mismatch at {path}:{index + 1}")
                path.unlink()
                counts[path.name] += 1
                migrated_lines += len(lines)
                raw_bytes += sum(len(line) for line in lines)
        except sqlite3.Error as exc:
            raise AgRouteError(f"route migration {config.state_root / DATABASE}: {exc}") from exc
        finally:
            db.close()

    return {
        "store": str(config.state_root),
        "plans_files": counts["plans.jsonl"],
        "routes_files": counts["routes.jsonl"],
        "lines": migrated_lines,
        "raw_bytes": raw_bytes,
    }


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        targets: list[tuple[Path, Path | None]] = [(args.directory, Path(args.state_dir) if args.state_dir else None)]
        if args.all_registered and not args.state_dir:
            spool = _spool_dir()
            if spool is not None:
                reg = _read_cached_json(spool / "repositories.json")
                if reg and isinstance(reg.get("repositories"), list):
                    seen = set()
                    targets = []
                    for item in reg["repositories"]:
                        if not isinstance(item, dict):
                            continue
                        cr, sr = item.get("capture_root"), item.get("state_root")
                        if isinstance(cr, str) and isinstance(sr, str) and sr not in seen and Path(sr).is_dir():
                            seen.add(sr)
                            targets.append((Path(cr), Path(sr)))
                    if not targets:
                        targets = [(args.directory, None)]

        summaries = []
        for proj_dir, st_dir in targets:
            cfg = load_config(
                args.config,
                project=proj_dir,
                state_directory=st_dir,
                include_user_config=not args.no_user_config,
            )
            if not (cfg.state_root / "store.json").is_file() and args.all_registered:
                continue
            summaries.append(migrate_store(cfg))

        payload = summaries[0] if len(summaries) == 1 and not args.all_registered else {"stores": summaries}
        if args.json:
            print(json.dumps(payload, sort_keys=True))
        else:
            for s in summaries:
                print(
                    f"Migrated {s['store']}: plans_files={s['plans_files']} "
                    f"routes_files={s['routes_files']} lines={s['lines']} raw_bytes={s['raw_bytes']}"
                )
        return 0
    except KeyboardInterrupt:
        print("migrate-routes: interrupted", file=sys.stderr)
        return 130
    except (AgRouteError, OSError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}), file=sys.stderr)
        else:
            print(f"migrate-routes: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

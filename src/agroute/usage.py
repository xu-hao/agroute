"""Lightweight native quota and rate-limit tracking across Codex, Claude Code, and Antigravity."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import time

from .discovery import _claude_home, _codex_home
from .files import atomic_write
from .state import check, claim, identity_locked
from .validation import (
    AGENTS,
    AgRouteAuthError,
    AgRouteError,
    agent_id,
    boolean,
    format_timestamp,
    keys,
    number,
    percentage,
    parse_timestamp,
    table,
    text,
)

USAGE_STORE_FORMAT = "agroute-usage-store-v1"
RESERVATIONS_STORE_FORMAT = "agroute-reservations-v1"
RESERVATION_TTL_SECONDS = 18000.0
PROBE_TIMEOUT_SECONDS = 15
REFRESH_ATTEMPTS = 2
# Patch marker: the report covers every quota window the agent currently has.
COMPLETE_REPORT = "complete_report"


def _empty_window():
    return {"used_pct": None, "reset_at": None}


def _empty_agent_state():
    return {
        "session_5h": _empty_window(),
        "weekly_7d": _empty_window(),
        "rate_limited": False,
        "cooldown_until": None,
        "tiers": {},
        "updated_at": None,
        "source": None,
        "detected_plan": None,
        "availability": _empty_window(),
        "scope_availability": {},
        "probe": {},
        "auth": {},
    }


def _empty_store():
    return {
        "format": USAGE_STORE_FORMAT,
        "agents": {name: _empty_agent_state() for name in AGENTS},
    }


def _now_utc(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if isinstance(now, datetime):
        if now.utcoffset() is None:
            raise AgRouteError("now: expected timezone-aware ISO-8601 timestamp")
        return now.astimezone(timezone.utc)
    return parse_timestamp(now, "now")


def _validate_window(win, where):
    table(win, where)
    keys(win, ["used_pct", "reset_at"], ["status", "observed_at", "measured_at", "event_id", "superseded_events"], where=where)
    used = win["used_pct"]
    if used is not None:
        used = percentage(used, f"{where}.used_pct")
    reset_at = win["reset_at"]
    if reset_at is not None:
        reset_at = format_timestamp(parse_timestamp(reset_at, f"{where}.reset_at"))
    result = {"used_pct": used, "reset_at": reset_at}
    if "status" in win:
        if win["status"] not in ("rejected", "allowed", "reported"):
            raise AgRouteError(f"{where}.status: unsupported availability status")
        result["status"] = win["status"]
    for key in ("observed_at", "measured_at"):
        if win.get(key) is not None:
            result[key] = format_timestamp(parse_timestamp(win[key], f"{where}.{key}"))
    if win.get("event_id") is not None:
        result["event_id"] = text(win["event_id"], f"{where}.event_id", max_length=64)
    if "superseded_events" in win:
        ids = win["superseded_events"]
        if not isinstance(ids, list) or any(not isinstance(value, str) or len(value) > 64 for value in ids):
            raise AgRouteError(f"{where}.superseded_events: expected event identities")
        result["superseded_events"] = list(dict.fromkeys(ids))
    return result


def _validate_probe(raw, where):
    table(raw, where)
    keys(raw, [], ["attempted_at", "succeeded_at", "failed_at", "error", "warnings"], where=where)
    result = {}
    for key in ("attempted_at", "succeeded_at", "failed_at"):
        if raw.get(key) is not None:
            result[key] = format_timestamp(parse_timestamp(raw[key], f"{where}.{key}"))
    if raw.get("error") is not None:
        result["error"] = text(raw["error"], f"{where}.error", max_length=4096)
    warnings = raw.get("warnings", [])
    if not isinstance(warnings, list) or any(not isinstance(value, str) for value in warnings):
        raise AgRouteError(f"{where}.warnings: expected warning strings")
    result["warnings"] = warnings
    return result


def _validate_auth(raw, where):
    table(raw, where)
    keys(raw, [], ["status", "observed_at", "error", "event_id", "superseded_events"], where=where)
    result = {}
    if raw.get("status") is not None:
        if raw["status"] not in ("expired", "logged_out"):
            raise AgRouteError(f"{where}.status: unsupported auth status")
        result["status"] = raw["status"]
    if raw.get("observed_at") is not None:
        result["observed_at"] = format_timestamp(parse_timestamp(raw["observed_at"], f"{where}.observed_at"))
    if raw.get("error") is not None:
        result["error"] = text(raw["error"], f"{where}.error", max_length=4096)
    if raw.get("event_id") is not None:
        result["event_id"] = text(raw["event_id"], f"{where}.event_id", max_length=64)
    if "superseded_events" in raw:
        ids = raw["superseded_events"]
        if not isinstance(ids, list) or any(not isinstance(value, str) or len(value) > 64 for value in ids):
            raise AgRouteError(f"{where}.superseded_events: expected event identities")
        result["superseded_events"] = list(dict.fromkeys(ids))
    return result


def _validate_store_payload(payload):
    table(payload, "usage store")
    keys(payload, ["format", "agents"], where="usage store")
    if payload["format"] != USAGE_STORE_FORMAT:
        raise AgRouteError(f"usage store: unsupported format {payload['format']!r}")
    agents_obj = table(payload["agents"], "usage store.agents")
    keys(agents_obj, AGENTS, where="usage store.agents")
    normalized = {"format": USAGE_STORE_FORMAT, "agents": {}}
    for name in AGENTS:
        where = f"usage store.agents.{name}"
        raw = table(agents_obj[name], where)
        keys(
            raw,
            ["session_5h", "weekly_7d", "rate_limited", "cooldown_until", "tiers", "updated_at", "source"],
            ["availability", "scope_availability", "probe", "detected_plan", "auth"],
            where=where,
        )
        s5 = _validate_window(raw["session_5h"], f"{where}.session_5h")
        w7 = _validate_window(raw["weekly_7d"], f"{where}.weekly_7d")
        rl = boolean(raw["rate_limited"], f"{where}.rate_limited")
        cd = raw["cooldown_until"]
        if cd is not None:
            cd = format_timestamp(parse_timestamp(cd, f"{where}.cooldown_until"))
        tiers_raw = table(raw["tiers"], f"{where}.tiers")
        tiers = {}
        for tier_name, tier_win in tiers_raw.items():
            text(tier_name, f"{where}.tiers.{tier_name}", label=True, max_length=64)
            tiers[tier_name] = _validate_window(tier_win, f"{where}.tiers.{tier_name}")
        scoped = {}
        for scope, win in table(raw.get("scope_availability", {}), f"{where}.scope_availability").items():
            text(scope, f"{where}.scope_availability.{scope}", label=True, max_length=57)
            scoped[scope] = _validate_window(win, f"{where}.scope_availability.{scope}")
        updated = raw["updated_at"]
        if updated is not None:
            updated = format_timestamp(parse_timestamp(updated, f"{where}.updated_at"))
        src = raw["source"]
        if src is not None:
            src = text(src, f"{where}.source", max_length=64)
        det_plan = raw.get("detected_plan")
        if det_plan is not None:
            det_plan = text(det_plan, f"{where}.detected_plan", max_length=64)
            from .config import normalize_plan_slug
            det_plan = normalize_plan_slug(name, det_plan) or det_plan
        normalized["agents"][name] = {
            "session_5h": s5,
            "weekly_7d": w7,
            "rate_limited": rl,
            "cooldown_until": cd,
            "tiers": tiers,
            "updated_at": updated,
            "source": src,
            "detected_plan": det_plan,
            "availability": _validate_window(raw.get("availability", _empty_window()), f"{where}.availability"),
            "scope_availability": scoped,
            "probe": _validate_probe(raw.get("probe", {}), f"{where}.probe"),
            "auth": _validate_auth(raw.get("auth", {}), f"{where}.auth"),
        }
        if name == "claude" and "availability" not in raw and "probe" not in raw:
            _migrate_legacy_claude_state(normalized["agents"][name])
    return normalized


def _migrate_legacy_claude_state(state):
    """Retain old telemetry cooldowns as ordered, recoverable availability evidence."""
    if not state["rate_limited"] or state["source"] not in ("probe", "ingest", "discovered"):
        return
    matches = [key for key in ("session_5h", "weekly_7d")
               if state["cooldown_until"] and state[key]["reset_at"] == state["cooldown_until"]]
    key = matches[0] if len(matches) == 1 else "availability"
    win = {"used_pct": None, "reset_at": state["cooldown_until"], "status": "rejected"}
    if state["updated_at"]:
        win["observed_at"] = state["updated_at"]
    state[key] = win
    state["rate_limited"], state["cooldown_until"] = False, None


@contextmanager
def _usage_locked(config):
    with identity_locked(config):
        config.state_root.mkdir(parents=True, exist_ok=True)
        lock_path = config.state_root / "usage.lock"
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                check(config)
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _discover_codex_live_patch() -> dict:
    """Discover latest rate_limits from ${CODEX_HOME:-~/.codex}/sessions rollout JSONL files."""
    sessions_dir = _codex_home() / "sessions"
    if not sessions_dir.is_dir():
        return {}
    try:
        candidates: list[Path] = []
        # Walk newest year/month/day directories first for speed
        for year_dir in sorted(sessions_dir.iterdir(), reverse=True):
            if not year_dir.is_dir():
                continue
            for month_dir in sorted(year_dir.iterdir(), reverse=True):
                if not month_dir.is_dir():
                    continue
                for day_dir in sorted(month_dir.iterdir(), reverse=True):
                    if not day_dir.is_dir():
                        continue
                    files = sorted(
                        day_dir.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime_ns,
                        reverse=True,
                    )
                    candidates.extend(files[:3])
                    if len(candidates) >= 3:
                        break
                if len(candidates) >= 3:
                    break
            if len(candidates) >= 3:
                break
        if not candidates:
            candidates = sorted(
                sessions_dir.rglob("*.jsonl"),
                key=lambda p: p.stat().st_mtime_ns,
                reverse=True,
            )[:3]
    except OSError:
        return {}

    for path in candidates:
        try:
            with path.open("rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                read_len = min(size, 256 * 1024)
                f.seek(size - read_len)
                tail = f.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for line in reversed(tail.splitlines()):
            if '"rate_limits"' not in line:
                continue
            patch: dict = {}
            if _parse_json_events(line, patch) and patch:
                # File touches do not refresh an old quota measurement.
                try:
                    observed = json.loads(line).get("timestamp")
                    if observed is not None:
                        observed = format_timestamp(parse_timestamp(observed, "codex event timestamp"))
                        for _, win in _patch_windows(patch):
                            win.setdefault("observed_at", observed)
                            if win.get("used_pct") is not None:
                                win.setdefault("measured_at", observed)
                except (ValueError, AgRouteError):
                    pass
                return patch
    return {}


def _is_valid_claude_assistant_turn(obj: dict) -> bool:
    if obj.get("type") != "assistant":
        return False
    if obj.get("error") or obj.get("isApiErrorMessage") or obj.get("is_error"):
        return False
    msg = obj.get("message")
    if not isinstance(msg, dict):
        return False
    if msg.get("model") == "<synthetic>" or msg.get("error"):
        return False
    return True


def _discover_claude_live_patch() -> dict:
    """Reconcile bounded recent transcript evidence by quota and event time."""
    projects_dir = _claude_home() / "projects"
    if not projects_dir.is_dir():
        return {}
    try:
        files = sorted(projects_dir.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime_ns, reverse=True)[:5]
    except OSError:
        return {}
    patch = {}
    latest_explicit_valid_ts: str | None = None
    auth_explicit_ts = False
    for path in reversed(files):
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                start_offset = max(0, size - 256 * 1024)
                handle.seek(start_offset)
                tail = handle.read().decode("utf-8", errors="replace")
            fallback_time = format_timestamp(datetime.fromtimestamp(path.stat().st_mtime, timezone.utc))
        except OSError:
            continue
        offset = start_offset
        for raw_line in tail.splitlines(keepends=True):
            line, line_offset = raw_line.strip(), offset
            offset += len(raw_line.encode("utf-8"))
            if not any(
                token in line
                for token in (
                    '"rate_limit_event"',
                    '"rate_limit_info"',
                    '"rateLimitInfo"',
                    '"authentication_failed"',
                    '"authentication_error"',
                    "Failed to authenticate",
                    '"api_error_status"',
                    '"loggedIn"',
                    '"usage_report"',
                    '"assistant"',
                )
            ):
                continue
            obj = None
            brace = line.find("{")
            if brace >= 0:
                try:
                    parsed = json.loads(line[brace:].strip())
                    if isinstance(parsed, dict):
                        obj = parsed
                except ValueError:
                    pass
            event_patch = {}
            if _parse_json_events(line, event_patch):
                ev_id = hashlib.sha256(f"{path}:{line_offset}:{line}".encode()).hexdigest()
                if isinstance(event_patch.get("auth"), dict):
                    auth_entry = deepcopy(event_patch["auth"])
                    explicit_ts = obj.get("timestamp") if isinstance(obj, dict) else None
                    auth_obs = auth_entry.setdefault("observed_at", fallback_time)
                    auth_entry["event_id"] = ev_id
                    if (
                        latest_explicit_valid_ts is not None
                        and explicit_ts is not None
                        and parse_timestamp(auth_obs) < parse_timestamp(latest_explicit_valid_ts)
                    ):
                        continue
                    prev_auth = patch.get("auth")
                    if (
                        not isinstance(prev_auth, dict)
                        or not prev_auth.get("observed_at")
                        or parse_timestamp(auth_obs) >= parse_timestamp(prev_auth["observed_at"])
                    ):
                        superseded = list(prev_auth.get("superseded_events", [])) if isinstance(prev_auth, dict) else []
                        if isinstance(prev_auth, dict) and prev_auth.get("event_id") and prev_auth["event_id"] != ev_id:
                            superseded.append(prev_auth["event_id"])
                        if superseded:
                            auth_entry["superseded_events"] = list(dict.fromkeys(superseded))[-256:]
                        patch["auth"] = auth_entry
                        auth_explicit_ts = explicit_ts is not None
                else:
                    for key, win in _patch_windows(event_patch):
                        win.setdefault("observed_at", fallback_time)
                        if win.get("used_pct") is not None:
                            win.setdefault("measured_at", win["observed_at"])
                        win["event_id"] = ev_id
                        previous = _get_window(patch, key)
                        _put_window(patch, key, _merge_window(previous, win))
                    patch.setdefault("warnings", []).extend(event_patch.get("warnings", []))
                    has_allowed = any(win.get("status") == "allowed" for _, win in _patch_windows(event_patch))
                    has_rejected = any(win.get("status") == "rejected" for _, win in _patch_windows(event_patch))
                    is_valid_usage_rep = isinstance(obj, dict) and isinstance(obj.get("usage_report"), dict)
                    if (has_allowed and not has_rejected) or is_valid_usage_rep:
                        valid_obs = (obj.get("timestamp") if isinstance(obj, dict) else None) or fallback_time
                        if isinstance(obj, dict) and obj.get("timestamp") is not None:
                            try:
                                ts_norm = format_timestamp(parse_timestamp(obj["timestamp"], "claude event timestamp"))
                                if latest_explicit_valid_ts is None or parse_timestamp(ts_norm) >= parse_timestamp(latest_explicit_valid_ts):
                                    latest_explicit_valid_ts = ts_norm
                            except AgRouteError:
                                pass
                        if isinstance(patch.get("auth"), dict):
                            auth_obs = patch["auth"].get("observed_at")
                            try:
                                if not auth_explicit_ts or not auth_obs or parse_timestamp(valid_obs) >= parse_timestamp(auth_obs):
                                    patch.pop("auth", None)
                                    auth_explicit_ts = False
                            except AgRouteError:
                                pass
            elif isinstance(obj, dict) and _is_valid_claude_assistant_turn(obj):
                valid_obs = obj.get("timestamp") or fallback_time
                if obj.get("timestamp") is not None:
                    try:
                        ts_norm = format_timestamp(parse_timestamp(obj["timestamp"], "claude event timestamp"))
                        if latest_explicit_valid_ts is None or parse_timestamp(ts_norm) >= parse_timestamp(latest_explicit_valid_ts):
                            latest_explicit_valid_ts = ts_norm
                    except AgRouteError:
                        pass
                if isinstance(patch.get("auth"), dict):
                    auth_obs = patch["auth"].get("observed_at")
                    try:
                        if not auth_explicit_ts or not auth_obs or parse_timestamp(valid_obs) >= parse_timestamp(auth_obs):
                            patch.pop("auth", None)
                            auth_explicit_ts = False
                    except AgRouteError:
                        pass
    return patch


def _live_usage_cache_file() -> Path | None:
    if os.environ.get("AGROUTE_DISABLE_SPOOL"):
        return None
    env = os.environ.get("AGROUTE_SPOOL_DIR")
    if env:
        return Path(env).expanduser().resolve() / "live_usage_cache.json"
    if os.environ.get("AGROUTE_STATE_DIR"):
        return None
    base = (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "agroute"
    ).resolve()
    return base / "live_usage_cache.json"


def _read_shared_probe_cache(agent_name: str) -> dict:
    cache_path = _live_usage_cache_file()
    if cache_path is None or not cache_path.is_file():
        return {}
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    agents_map = raw.get("agents")
    if not isinstance(agents_map, dict):
        return {}
    entry = agents_map.get(agent_name)
    return entry if isinstance(entry, dict) else {}


def _write_shared_probe_cache(agent_name: str, state: dict) -> None:
    cache_path = _live_usage_cache_file()
    if cache_path is None:
        return
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if cache_path.is_file():
            try:
                existing = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                existing = {}
        if not isinstance(existing, dict):
            existing = {}
        agents_map = existing.get("agents")
        if not isinstance(agents_map, dict):
            agents_map = {}
        agents_map[agent_name] = deepcopy(state)
        existing["agents"] = agents_map
        atomic_write(cache_path, json.dumps(existing, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def _clear_shared_probe_cache(agent_names) -> None:
    cache_path = _live_usage_cache_file()
    if cache_path is None or not cache_path.is_file():
        return
    try:
        existing = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    agents_map = existing.get("agents") if isinstance(existing, dict) else None
    if not isinstance(agents_map, dict) or not any(name in agents_map for name in agent_names):
        return
    for name in agent_names:
        agents_map.pop(name, None)
    atomic_write(cache_path, json.dumps(existing, indent=2, sort_keys=True) + "\n")


def discover_live_usage_patch(agent_name: str) -> dict:
    from .adapters import get_adapter

    try:
        return get_adapter(agent_name).discover_live_usage_patch()
    except Exception:
        return _read_shared_probe_cache(agent_name)


def _read_raw_store(config):
    check(config)
    path = config.state_root / "usage.json"
    if not path.exists():
        return _empty_store()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise AgRouteError(f"usage store: invalid JSON at {path}: {exc}") from exc
    return _validate_store_payload(raw)


def read_store(config):
    store = _read_raw_store(config)
    for name in AGENTS:
        ag = store["agents"][name]
        if name == "claude":
            patch = _discover_claude_live_patch()
            # A cache is fallback data, never a new successful probe.
            if ag["updated_at"] is None and not patch:
                cached = _read_shared_probe_cache(name)
                if cached:
                    cached_auth = _validate_auth(cached.get("auth", {}), "cached.claude.auth")
                    local_auth = ag.get("auth") or {}
                    local_fail = ag.get("probe", {}).get("failed_at")
                    cached_success = (cached.get("probe") or {}).get("succeeded_at") or cached.get("updated_at")
                    local_block_time = local_auth.get("observed_at") or local_fail
                    if not local_block_time or (
                        cached_success
                        and not cached_auth.get("status")
                        and parse_timestamp(cached_success) > parse_timestamp(local_block_time)
                    ):
                        for key, win in _patch_windows(cached):
                            _put_window(ag, key, _validate_window(win, f"cached.claude.{key}"))
                        for key in ("updated_at", "source", "rate_limited", "cooldown_until", "probe", "detected_plan", "auth"):
                            if key in cached:
                                ag[key] = deepcopy(cached[key])
                        if "auth" in ag:
                            ag["auth"] = _validate_auth(ag["auth"], "cached.claude.auth")
                        if "availability" not in cached and "probe" not in cached:
                            _migrate_legacy_claude_state(ag)
                    elif cached_auth.get("status") and (
                        not local_block_time
                        or (
                            cached_auth.get("observed_at")
                            and parse_timestamp(cached_auth["observed_at"]) >= parse_timestamp(local_block_time)
                        )
                    ):
                        ag["auth"] = cached_auth
            for key, win in _patch_windows(patch):
                _put_window(ag, key, _merge_window(_get_window(ag, key), win))
            if isinstance(patch.get("auth"), dict) and patch["auth"].get("status"):
                t_auth = _validate_auth(patch["auth"], "discovered.claude.auth")
                stored_auth = ag.get("auth") or {}
                superseded = set(stored_auth.get("superseded_events", []))
                probe_succeeded = ag.get("probe", {}).get("succeeded_at")
                valid_clocks = [t for t in (probe_succeeded, ag.get("updated_at")) if t]
                newest_valid = max(valid_clocks, key=parse_timestamp) if valid_clocks else None
                t_obs = t_auth.get("observed_at")
                already_superseded = (
                    (t_auth.get("event_id") is not None and t_auth["event_id"] in superseded)
                    or (newest_valid is not None and t_obs is not None and parse_timestamp(newest_valid) >= parse_timestamp(t_obs))
                    or (stored_auth.get("observed_at") is not None and t_obs is not None and parse_timestamp(t_obs) < parse_timestamp(stored_auth["observed_at"]))
                )
                if not already_superseded:
                    merged_auth = deepcopy(t_auth)
                    if superseded:
                        merged_auth["superseded_events"] = list(
                            dict.fromkeys(list(superseded) + merged_auth.get("superseded_events", []))
                        )[-256:]
                    ag["auth"] = merged_auth
            if patch.get("detected_plan") is not None:
                ag["detected_plan"] = patch["detected_plan"]
            times = [win["observed_at"] for _, win in _patch_windows(ag) if win.get("observed_at")]
            if times:
                ag["updated_at"] = max(times + ([ag["updated_at"]] if ag["updated_at"] else []), key=parse_timestamp)
            if patch and list(_patch_windows(patch)):
                ag["source"] = "discovered" if ag["updated_at"] is None else ag["source"]
            continue
        # Passive evidence has its own window clocks and never represents a
        # local probe attempt. Reconcile both sources independently, allowing
        # passive windows to advance only strictly newer local windows.
        for patch in (_read_shared_probe_cache(name), discover_live_usage_patch(name)):
            patch_advanced = False
            for key, win in _patch_windows(patch):
                validated = _validate_window(win, f"discovered.{name}.{key}")
                previous = _get_window(ag, key)
                prev_obs = previous.get("observed_at")
                new_obs = validated.get("observed_at")
                if not new_obs and (_window_has_evidence(previous) or ag["updated_at"]):
                    continue
                if prev_obs and new_obs and parse_timestamp(new_obs) <= parse_timestamp(prev_obs):
                    continue
                _put_window(ag, key, _merge_window(previous, validated))
                patch_advanced = True
                if ag["source"] is None:
                    ag["source"] = "discovered"
            if patch and ag["updated_at"] is None:
                if "rate_limited" in patch and not ag["rate_limited"]:
                    ag["rate_limited"] = bool(patch["rate_limited"])
                if "cooldown_until" in patch and ag["cooldown_until"] is None:
                    ag["cooldown_until"] = patch["cooldown_until"]
            if patch.get("detected_plan") is not None and (ag["detected_plan"] is None or patch_advanced):
                ag["detected_plan"] = patch["detected_plan"]
        times = [win["observed_at"] for _, win in _patch_windows(ag) if win.get("observed_at")]
        if times:
            ag["updated_at"] = max(times + ([ag["updated_at"]] if ag["updated_at"] else []), key=parse_timestamp)
    return store


def write_store(config, store):
    validated = _validate_store_payload(store)
    claim(config)
    path = config.state_root / "usage.json"
    atomic_write(path, json.dumps(validated, indent=2, sort_keys=True) + "\n")
    return validated


def _patch_windows(patch):
    for key in ("session_5h", "weekly_7d", "availability"):
        if isinstance(patch.get(key), dict):
            yield key, patch[key]
    for container in ("tiers", "scope_availability"):
        for key, win in patch.get(container, {}).items():
            yield container + "." + key, win


def _get_window(state, key):
    if "." in key:
        container, name = key.split(".", 1)
        return state.get(container, {}).get(name, _empty_window())
    return state.get(key, _empty_window())


def _put_window(state, key, win):
    if "." in key:
        container, name = key.split(".", 1)
        state.setdefault(container, {})[name] = win
    else:
        state[key] = win


def _merge_window(previous, incoming):
    """Only newer evidence for this exact quota can supersede its availability."""
    old_time, new_time = previous.get("observed_at"), incoming.get("observed_at")
    ignored = previous.get("superseded_events", [])
    if incoming.get("event_id") in ignored:
        return deepcopy(previous)
    if old_time and new_time and parse_timestamp(new_time) < parse_timestamp(old_time):
        return deepcopy(previous)
    result = deepcopy(previous)
    if "status" in incoming:
        result = deepcopy(incoming)
        if (incoming["status"] == "allowed" and incoming.get("used_pct") is None
                and previous.get("status") != "rejected" and previous.get("used_pct") is not None):
            # Ordinary accepted work does not refresh or erase a real counter.
            # Recovery from rejection, by contrast, leaves unknown usage null.
            result["used_pct"] = previous["used_pct"]
            measured_at = previous.get("measured_at") or previous.get("observed_at")
            if measured_at:
                result["measured_at"] = measured_at
            result["reset_at"] = incoming.get("reset_at") or previous.get("reset_at")
    else:
        for key in ("used_pct", "reset_at"):
            if incoming.get(key) is not None:
                result[key] = incoming[key]
        for key in ("observed_at", "measured_at"):
            if incoming.get(key) is not None:
                result[key] = incoming[key]
    superseded = list(dict.fromkeys(ignored + incoming.get("superseded_events", [])))
    if previous.get("event_id") and previous.get("event_id") != incoming.get("event_id"):
        superseded.append(previous["event_id"])
    if superseded:
        result["superseded_events"] = list(dict.fromkeys(superseded))[-256:]
    return result


def _window_rejection_reason(agent, group, s5, w7):
    for label, win in (("session", s5), ("weekly", w7)):
        if win.get("status") == "rejected":
            scope = "" if group == "shared" else f" {group}"
            until = f" until {win['reset_at']}" if win["reset_at"] else ""
            return f"{agent}{scope} {label} limit rejected{until}"
    return None


def _rejection_reason(agent, state):
    reason = _window_rejection_reason(agent, "shared", state["session_5h"], state["weekly_7d"])
    availability = state.get("availability", {})
    if reason:
        return reason
    if availability.get("status") == "rejected":
        until = f" until {availability['reset_at']}" if availability.get("reset_at") else ""
        return f"{agent} access rejected{until}"
    return None


def _scoped_access_rejection(agent, state, candidate):
    if agent != "claude":
        return None
    model = str(candidate["model"]).lower()
    tokens = [token for token in re.split(r"[-_./:]+", model)
              if len(token) >= 3 and token not in ("claude", "code")]
    keys_to_check = [model, str(candidate.get("tier", "")).lower(), *tokens]
    for scope, win in state.get("scope_availability", {}).items():
        if win.get("status") == "rejected" and any(
            key and (scope == key or scope.startswith(key + "-")) for key in keys_to_check
        ):
            until = f" until {win['reset_at']}" if win["reset_at"] else ""
            return f"{agent} {scope} access rejected{until}"
    return None


def _auth_failure_reason(agent, state):
    auth = state.get("auth")
    if isinstance(auth, dict) and auth.get("status") in ("expired", "logged_out"):
        return auth.get("error") or (
            f"{agent} login expired; run '{agent} auth login' to sign in again"
            if auth.get("status") == "expired"
            else f"{agent} is not logged in; run '{agent} auth login' to authenticate"
        )
    return None


def usage_warnings(agent, state, cfg, now_dt):
    probe = state.get("probe", {})
    success = probe.get("succeeded_at")
    data_time = state.get("updated_at")
    references = [value for value in (success, data_time) if value]
    reference = min(references, key=parse_timestamp) if references else None
    stale = reference is None or (now_dt - parse_timestamp(reference)).total_seconds() > cfg["usage_stale_after_seconds"]
    failed = probe.get("failed_at") and (not success or parse_timestamp(probe["failed_at"]) >= parse_timestamp(success))
    warnings = list(probe.get("warnings", []))
    auth_reason = _auth_failure_reason(agent, state)
    if auth_reason and not (failed and auth_reason in str(probe.get("error", ""))):
        warnings.append(auth_reason)
    if stale or failed:
        message = f"{agent} usage snapshot stale since {reference or 'unknown'}" if stale else f"{agent} usage snapshot uncertain"
        if failed:
            message += f"; last probe failed at {probe['failed_at']}: {probe.get('error', 'unknown error')}"
        warnings.append(message)
    return warnings


def visible_agent_state(agent, state, cfg, now_dt):
    result = effective_agent_state(state, now_dt)
    probe = result.get("probe", {})
    success, failure = probe.get("succeeded_at"), probe.get("failed_at")
    if failure and (not success or parse_timestamp(failure) >= parse_timestamp(success)):
        for _, win in _patch_windows(result):
            measured_at = win.get("measured_at") or win.get("observed_at") or result.get("updated_at")
            if measured_at is None or (now_dt - parse_timestamp(measured_at)).total_seconds() > cfg["usage_stale_after_seconds"]:
                win["used_pct"] = None
    return result


def _window_has_evidence(win):
    return win.get("used_pct") is not None or win.get("status") is not None or win.get("reset_at") is not None


def _current_probe_evidence(patch, cfg, now_dt):
    """Operation success alone cannot refresh a cached or empty report."""
    reference = patch.get("updated_at") or format_timestamp(now_dt)
    for _, win in _patch_windows(patch):
        if not _window_has_evidence(win):
            continue
        if win.get("reset_at") and now_dt >= parse_timestamp(win["reset_at"]):
            continue
        observed = win.get("measured_at") if win.get("used_pct") is not None else None
        observed = observed or win.get("observed_at") or reference
        if 0 <= (now_dt - parse_timestamp(observed)).total_seconds() <= cfg["usage_stale_after_seconds"]:
            return True
    if "rate_limited" in patch or patch.get("cooldown_until"):
        return 0 <= (now_dt - parse_timestamp(reference)).total_seconds() <= cfg["usage_stale_after_seconds"]
    return False


def effective_stale_after_seconds(key, win, cfg, *, is_host=False):
    """Scale freshness down to the low-headroom floor as a usable window nears its cutoff."""
    base = float(cfg["usage_stale_after_seconds"])
    threshold = float(cfg.get("usage_low_headroom_pct", 20.0))
    floor = min(base, float(cfg.get("usage_low_headroom_stale_after_seconds", 60.0)))
    used = win.get("used_pct") if isinstance(win, dict) else None
    if threshold <= 0.0 or used is None:
        return base
    reserve = float(cfg.get("host_reserve_pct", 0.0)) if is_host else 0.0
    if key == "session_5h":
        limit = max(0.0, float(cfg["max_session_used_pct"]) - reserve)
    elif key == "weekly_7d":
        limit = max(0.0, float(cfg["max_weekly_used_pct"]) - reserve)
    elif key.startswith("tiers."):
        win_limit = float(cfg["max_session_used_pct"] if key.endswith("-5h") else cfg["max_weekly_used_pct"])
        limit = min(float(cfg["max_tier_used_pct"]), max(0.0, win_limit - reserve))
    else:
        limit = 100.0
    headroom = limit - float(used)
    if 0.0 < headroom < threshold:
        return floor
    return base


def _freshness_reason(state, cfg, now_dt, *, is_host=False):
    """Check each known scope; a recent sibling never renews its clock."""
    auth_reason = _auth_failure_reason("agent", state)
    if auth_reason:
        return auth_reason
    probe = state.get("probe", {})
    failed, succeeded = probe.get("failed_at"), probe.get("succeeded_at")
    if failed and (not succeeded or parse_timestamp(failed) >= parse_timestamp(succeeded)):
        return f"last probe failed at {failed}: {probe.get('error', 'unknown error')}"
    found = False
    for key, win in _patch_windows(state):
        if not _window_has_evidence(win):
            continue
        found = True
        if win.get("reset_at") and now_dt >= parse_timestamp(win["reset_at"]):
            return f"{key} evidence expired at {win['reset_at']}"
        if (
            (key.startswith("tiers.") or key.startswith("scope_availability."))
            and win.get("status") == "rejected"
            and win.get("reset_at") is not None
            and now_dt < parse_timestamp(win["reset_at"])
        ):
            # An explicit, unexpired tier/scoped refusal remains authoritative until reset or probe recovery.
            continue
        observed = win.get("measured_at") if win.get("used_pct") is not None else None
        observed = observed or win.get("observed_at") or state.get("updated_at")
        max_age = effective_stale_after_seconds(key, win, cfg, is_host=is_host)
        if observed is None or not 0 <= (now_dt - parse_timestamp(observed)).total_seconds() <= max_age:
            return f"{key} evidence stale since {observed or 'unknown'}"
    # Explicit global availability/cooldown evidence has the snapshot's clock.
    if state.get("rate_limited") or state.get("cooldown_until") or not found:
        observed = state.get("updated_at")
        if state.get("cooldown_until") and now_dt >= parse_timestamp(state["cooldown_until"]):
            return "rate-limit evidence expired"
        if observed is None or not 0 <= (now_dt - parse_timestamp(observed)).total_seconds() <= cfg["usage_stale_after_seconds"]:
            return f"quota evidence stale since {observed or 'unknown'}"
    return None


def _fresh_global_block(state, cfg, now_dt):
    """Current whole-provider denial needs no claim about remaining scopes."""
    if _auth_failure_reason("agent", state):
        return False
    for key in ("session_5h", "weekly_7d", "availability"):
        win = state.get(key, {})
        if win.get("reset_at") and now_dt >= parse_timestamp(win["reset_at"]):
            continue
        observed = win.get("observed_at") or state.get("updated_at")
        if win.get("status") == "rejected" and observed and 0 <= (now_dt - parse_timestamp(observed)).total_seconds() <= cfg["usage_stale_after_seconds"]:
            return True
        measured = win.get("measured_at") or win.get("observed_at") or state.get("updated_at")
        if win.get("used_pct") is not None and measured and 0 <= (now_dt - parse_timestamp(measured)).total_seconds() <= cfg["usage_stale_after_seconds"]:
            threshold = cfg.get("max_session_used_pct" if key == "session_5h" else "max_weekly_used_pct")
            if key != "availability" and win["used_pct"] >= threshold:
                return True
    return False


def routing_freshness_reason(state, cfg, now_dt, *, is_host=False):
    """Availability requires current scopes; current denial can safely block."""
    return None if _fresh_global_block(state, cfg, now_dt) else _freshness_reason(state, cfg, now_dt, is_host=is_host)


def ensure_fresh_usage(config, targets, *, host=None, now=None):
    """Refresh routing inputs with one retry, isolating infrastructure failures."""
    store = read_store(config)
    names = list(dict.fromkeys(targets))
    attempts = dict.fromkeys(names, 0)
    failures = {}
    while True:
        now_dt = _now_utc(now)
        pending = []
        for name in names:
            cfg, state = config.agents[name], store["agents"][name]
            is_host = name == host
            if not cfg["enabled"] or _fresh_global_block(state, cfg, now_dt) or _freshness_reason(state, cfg, now_dt, is_host=is_host) is None:
                failures.pop(name, None)
            else:
                pending.append(name)
        if not any(attempts[name] < REFRESH_ATTEMPTS for name in pending):
            for name in pending:
                if name not in failures:
                    reason = _freshness_reason(store["agents"][name], config.agents[name], now_dt, is_host=(name == host))
                    exc = AgRouteError(f"usage probe: refresh budget exhausted with {reason}")
                    _record_probe_failure(config, name, exc, now)
                    failures[name] = (f"{name} quota refresh infrastructure incident after {REFRESH_ATTEMPTS} attempts: "
                                      f"{exc}; retry with agr usage probe --agent {name}")
            break
        for name in pending:
            if attempts[name] >= REFRESH_ATTEMPTS:
                continue
            attempts[name] += 1
            cfg = config.agents[name]
            try:
                probe_usage(config, name, now=now)
                store = read_store(config)
                now_dt = _now_utc(now)
                state = store["agents"][name]
                reason = None if _fresh_global_block(state, cfg, now_dt) else _freshness_reason(state, cfg, now_dt, is_host=(name == host))
                if reason is not None:
                    raise AgRouteError(f"usage probe: {reason} after partial report")
            except AgRouteAuthError as exc:
                _record_probe_failure(config, name, exc, now)
                attempts[name] = REFRESH_ATTEMPTS
                failures[name] = f"{name} authentication unavailable: {exc.auth_reason}"
            except AgRouteError as exc:
                _record_probe_failure(config, name, exc, now)
                failures[name] = (f"{name} quota refresh infrastructure incident after {REFRESH_ATTEMPTS} attempts: "
                                  f"{exc}; retry with agr usage probe --agent {name}")
            else:
                failures.pop(name, None)
        # Another provider's probe may cross this provider's reset or age
        # boundary. Recheck everyone without renewing any attempt budget.
        store = read_store(config)
    final_store = read_store(config)
    if (config.state_root / "store.json").exists():
        from .calibration import record_usage_calibration_snapshot

        now_cal = _now_utc(now)
        with _usage_locked(config):
            for name in names:
                record_usage_calibration_snapshot(config, name, final_store["agents"][name], now_cal)
    return final_store, failures


def effective_agent_state(agent_state, now_dt, *, project_rejections=True):
    state = deepcopy(agent_state)
    state.setdefault("detected_plan", None)
    for win_key in ("session_5h", "weekly_7d", "availability"):
        win = state.setdefault(win_key, _empty_window())
        if win["reset_at"] is not None and now_dt >= parse_timestamp(win["reset_at"]):
            state[win_key] = _empty_window()
    if state["cooldown_until"] is not None and now_dt >= parse_timestamp(state["cooldown_until"]):
        state["cooldown_until"] = None
        state["rate_limited"] = False
    for container in ("tiers", "scope_availability"):
        state[container] = {
            name: win for name, win in state.get(container, {}).items()
            if win["reset_at"] is None or now_dt < parse_timestamp(win["reset_at"])
        }
    if project_rejections:
        for key in ("session_5h", "weekly_7d", "availability"):
            win = state[key]
            if win.get("status") == "rejected":
                state["rate_limited"] = True
                state["cooldown_until"] = win["reset_at"]
                break
    return state


def evaluate_agent_quota(agent_name, agent_cfg, effective_state, *, is_host=False):
    """Return (eligible: bool, reason: str | None) for the agent as a whole."""
    if not agent_cfg["enabled"]:
        return False, "agent is disabled in configuration"
    auth_reason = _auth_failure_reason(agent_name, effective_state)
    if auth_reason:
        return False, auth_reason
    rejection = _rejection_reason(agent_name, effective_state)
    if rejection:
        return False, rejection
    if effective_state["rate_limited"]:
        until = effective_state["cooldown_until"]
        suffix = f" until {until}" if until else ""
        return False, f"agent is rate-limited{suffix}"
    if effective_state["cooldown_until"] is not None:
        return False, f"agent is in cooldown until {effective_state['cooldown_until']}"

    reserve = agent_cfg["host_reserve_pct"] if is_host else 0.0
    max_session = max(0.0, agent_cfg["max_session_used_pct"] - reserve)
    max_weekly = max(0.0, agent_cfg["max_weekly_used_pct"] - reserve)

    s_used = effective_state["session_5h"]["used_pct"]
    if s_used is not None and s_used >= max_session:
        return (
            False,
            f"session_5h used_pct ({s_used:.1f}%) >= effective limit ({max_session:.1f}%)",
        )
    w_used = effective_state["weekly_7d"]["used_pct"]
    if w_used is not None and w_used >= max_weekly:
        return (
            False,
            f"weekly_7d used_pct ({w_used:.1f}%) >= effective limit ({max_weekly:.1f}%)",
        )
    return True, None


def evaluate_candidate_tier(agent_cfg, effective_state, candidate):
    """Return (eligible: bool, reason: str | None) for a candidate's model/tier window."""
    max_tier = agent_cfg["max_tier_used_pct"]
    keys_to_check = [candidate["model"]]
    if "tier" in candidate and candidate["tier"] not in keys_to_check:
        keys_to_check.append(candidate["tier"])
    model_tokens = [
        tok
        for tok in re.split(r"[-_./:]+", str(candidate["model"]).lower())
        if len(tok) >= 3
        and tok not in ("gemini", "claude", "gpt", "low", "medium", "high", "xhigh", "max", "standard")
    ]
    for tok in model_tokens:
        if tok not in keys_to_check:
            keys_to_check.append(tok)
    tiers = effective_state.get("tiers") or {}
    for key in keys_to_check:
        for tier_key in (key, f"{key}-5h", f"{key}-weekly"):
            tier_win = tiers.get(tier_key)
            if tier_win is None:
                continue
            if tier_win.get("status") == "rejected":
                until = f" until {tier_win['reset_at']}" if tier_win.get("reset_at") else ""
                return False, f"tier '{tier_key}' limit rejected{until}"
            if tier_win.get("used_pct") is not None and tier_win["used_pct"] >= max_tier:
                return (
                    False,
                    f"tier '{tier_key}' used_pct ({tier_win['used_pct']:.1f}%) >= max_tier_used_pct ({max_tier:.1f}%)",
                )
    return True, None


def _reservations_path(config) -> Path:
    return config.state_root / "reservations.json"


def _validate_reservation_entry(raw: dict, where: str) -> dict:
    table(raw, where)
    keys(
        raw,
        ["route_id", "agent", "model", "reserved_credit_usd", "created_at", "pools"],
        ["decision_id"],
        where=where,
    )
    route_id = text(raw["route_id"], f"{where}.route_id", max_length=128)
    agent = agent_id(raw["agent"], f"{where}.agent")
    model = text(raw["model"], f"{where}.model", max_length=256)
    amount = number(raw["reserved_credit_usd"], f"{where}.reserved_credit_usd", positive=True)
    created_at = format_timestamp(parse_timestamp(raw["created_at"], f"{where}.created_at"))
    decision_id = raw.get("decision_id")
    if decision_id is not None:
        decision_id = text(decision_id, f"{where}.decision_id", max_length=128)
    pools_raw = raw["pools"]
    if not isinstance(pools_raw, list):
        raise AgRouteError(f"{where}.pools: expected a list")
    pools = []
    for idx, entry in enumerate(pools_raw):
        p_where = f"{where}.pools[{idx}]"
        table(entry, p_where)
        keys(entry, ["pool_group", "window"], ["reset_at"], where=p_where)
        pg = text(entry["pool_group"], f"{p_where}.pool_group", label=True, max_length=64).lower()
        win = text(entry["window"], f"{p_where}.window", label=True, max_length=32)
        if win not in ("session_5h", "weekly_7d"):
            raise AgRouteError(f"{p_where}.window: expected 'session_5h' or 'weekly_7d'")
        reset_at = entry.get("reset_at")
        if reset_at is not None:
            reset_at = format_timestamp(parse_timestamp(reset_at, f"{p_where}.reset_at"))
        pools.append({"pool_group": pg, "window": win, "reset_at": reset_at})
    out = {
        "route_id": route_id,
        "agent": agent,
        "model": model,
        "reserved_credit_usd": round(float(amount), 6),
        "created_at": created_at,
        "pools": pools,
    }
    if decision_id is not None:
        out["decision_id"] = decision_id
    return out


def read_reservations(config) -> list[dict]:
    """Read and validate `<state_root>/reservations.json`, or return an empty list."""
    check(config)
    path = _reservations_path(config)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise AgRouteError(f"reservations store: invalid JSON at {path}: {exc}") from exc
    table(raw, "reservations store")
    keys(raw, ["format", "reservations"], where="reservations store")
    if raw["format"] != RESERVATIONS_STORE_FORMAT:
        raise AgRouteError(f"reservations store: unsupported format {raw['format']!r}")
    items = raw["reservations"]
    if not isinstance(items, list):
        raise AgRouteError("reservations store.reservations: expected a list")
    return [_validate_reservation_entry(item, f"reservations[{idx}]") for idx, item in enumerate(items)]


def _write_reservations(config, reservations: list[dict]) -> None:
    """Persist `<state_root>/reservations.json` (caller holds `_usage_locked`)."""
    normalized = [_validate_reservation_entry(item, f"reservations[{idx}]") for idx, item in enumerate(reservations)]
    claim(config)
    atomic_write(
        _reservations_path(config),
        json.dumps({"format": RESERVATIONS_STORE_FORMAT, "reservations": normalized}, indent=2, sort_keys=True) + "\n",
    )


def record_credit_reservation_locked(
    config,
    *,
    route_id: str,
    decision_id: str | None,
    agent: str,
    model: str,
    pools: list[dict],
    reserved_credit_usd: float,
    created_at: str,
    now_dt=None,
) -> None:
    """Record a pending in-flight credit reservation while caller holds `_usage_locked`."""
    if not pools or reserved_credit_usd <= 0.0:
        return
    current = read_reservations(config)
    if now_dt is not None:
        kept = []
        for entry in current:
            created_dt = parse_timestamp(entry["created_at"])
            if (now_dt - created_dt).total_seconds() > RESERVATION_TTL_SECONDS:
                continue
            rem_pools = [
                p for p in entry["pools"]
                if p.get("reset_at") is None or now_dt < parse_timestamp(p["reset_at"])
            ]
            if rem_pools:
                entry = deepcopy(entry)
                entry["pools"] = rem_pools
                kept.append(entry)
        current = kept
    entry = {
        "route_id": route_id,
        "agent": agent,
        "model": model,
        "reserved_credit_usd": round(float(reserved_credit_usd), 6),
        "created_at": created_at,
        "pools": deepcopy(pools),
    }
    if decision_id:
        entry["decision_id"] = decision_id
    current.append(entry)
    _write_reservations(config, current)


def _pool_matches_tier_key(pool_group: str, tier_base: str) -> bool:
    return tier_base == pool_group or tier_base.startswith(pool_group + "-")


def _release_reservations_for_patch(config, agent: str, patch: dict) -> None:
    """Release reservations covered by fresh percentage measurements in `patch` (caller holds `_usage_locked`)."""
    if not _reservations_path(config).exists():
        return
    current = read_reservations(config)
    if not any(entry["agent"] == agent for entry in current):
        return
    if patch.get(COMPLETE_REPORT):
        kept = [entry for entry in current if entry["agent"] != agent]
        if len(kept) != len(current):
            _write_reservations(config, kept)
        return

    released_exact: set[tuple[str, str]] = set()
    released_tier_bases: list[tuple[str, str]] = []
    if isinstance(patch.get("session_5h"), dict) and patch["session_5h"].get("used_pct") is not None:
        released_exact.add(("shared", "session_5h"))
        if agent == "agy":
            released_exact.add(("gemini", "session_5h"))
    if isinstance(patch.get("weekly_7d"), dict) and patch["weekly_7d"].get("used_pct") is not None:
        released_exact.add(("shared", "weekly_7d"))
        if agent == "agy":
            released_exact.add(("gemini", "weekly_7d"))
    for tk, twin in (patch.get("tiers") or {}).items():
        if not isinstance(twin, dict) or twin.get("used_pct") is None:
            continue
        low = str(tk).lower()
        if low.endswith("-5h"):
            released_tier_bases.append((low[:-3], "session_5h"))
        elif low.endswith("-weekly"):
            released_tier_bases.append((low[:-7], "weekly_7d"))
        else:
            released_tier_bases.append((low, "weekly_7d"))

    if not released_exact and not released_tier_bases:
        return

    changed = False
    kept = []
    for entry in current:
        if entry["agent"] != agent:
            kept.append(entry)
            continue
        rem_pools = []
        for p in entry["pools"]:
            key = (p["pool_group"], p["window"])
            hit = key in released_exact or any(
                win == p["window"] and _pool_matches_tier_key(p["pool_group"], base)
                for base, win in released_tier_bases
            )
            if hit:
                changed = True
            else:
                rem_pools.append(p)
        if rem_pools:
            updated_entry = deepcopy(entry)
            updated_entry["pools"] = rem_pools
            kept.append(updated_entry)
        else:
            changed = True
    if changed:
        _write_reservations(config, kept)


def _clear_reservations(config, targets: list[str]) -> None:
    """Drop all reservations for cleared agents (caller holds `_usage_locked`)."""
    if not _reservations_path(config).exists():
        return
    current = read_reservations(config)
    target_set = set(targets)
    kept = [entry for entry in current if entry["agent"] not in target_set]
    if len(kept) != len(current):
        _write_reservations(config, kept)


def reconcile_reservation_on_close_locked(
    config,
    route_id: str,
    *,
    close_status: str,
    credit_usd: float | None = None,
) -> None:
    """Release or update an in-flight credit reservation when `agr close` closes `route_id`."""
    current = read_reservations(config)
    if not current:
        return
    kept: list[dict] = []
    changed = False
    for entry in current:
        if entry["route_id"] != route_id:
            kept.append(entry)
            continue
        if close_status == "cancelled" or (credit_usd is not None and credit_usd <= 0.0):
            changed = True
            continue
        if credit_usd is not None and credit_usd > 0.0:
            updated = deepcopy(entry)
            updated["reserved_credit_usd"] = round(float(credit_usd), 6)
            kept.append(updated)
            changed = True
            continue
        kept.append(entry)
    if changed:
        _write_reservations(config, kept)


def _state_window_for_pool(agent_name: str, effective_state: dict, pool_group: str, window: str) -> dict:
    if pool_group == "shared":
        return effective_state.get(window) or _empty_window()
    tiers = effective_state.get("tiers") or {}
    if window == "session_5h":
        for candidate_key in (f"{pool_group}-5h",):
            if candidate_key in tiers:
                return tiers[candidate_key]
        for tk, twin in tiers.items():
            if tk.endswith("-5h") and _pool_matches_tier_key(pool_group, tk[:-3]):
                return twin
        if agent_name == "agy" and pool_group == "gemini":
            return effective_state.get("session_5h") or _empty_window()
        return _empty_window()
    for candidate_key in (f"{pool_group}-weekly", pool_group):
        if candidate_key in tiers:
            return tiers[candidate_key]
    for tk, twin in tiers.items():
        if tk.endswith("-weekly") and _pool_matches_tier_key(pool_group, tk[:-7]):
            return twin
    if agent_name == "agy" and pool_group == "gemini":
        return effective_state.get("weekly_7d") or _empty_window()
    return _empty_window()


def active_reserved_credit_by_pool(config, agent_name: str, effective_state: dict, now_dt) -> dict[tuple[str, str], float]:
    """Sum active in-flight credit reservations per `(pool_group, window)` for `agent_name`."""
    entries = [e for e in read_reservations(config) if e["agent"] == agent_name]
    if not entries:
        return {}
    from .calibration import _load_hh_observations

    obs_by_decision: dict[str, dict] = {}
    hh_candidates = [config.state_root.parent / "howhard", config.capture_root / ".howhard"]
    if not getattr(config, "explicit_state", False):
        hh_candidates.insert(1, config.capture_root / ".git" / "howhard")
    for cand in hh_candidates:
        if cand.is_dir():
            dec_map, _ = _load_hh_observations(cand)
            obs_by_decision.update(dec_map)
    totals: dict[tuple[str, str], float] = {}
    for entry in entries:
        created_dt = parse_timestamp(entry["created_at"])
        age = (now_dt - created_dt).total_seconds()
        if age < 0 or age > RESERVATION_TTL_SECONDS:
            continue
        decision_id = entry.get("decision_id")
        obs = obs_by_decision.get(decision_id) if decision_id else None
        for p in entry["pools"]:
            if p.get("reset_at") is not None and now_dt >= parse_timestamp(p["reset_at"]):
                continue
            win = _state_window_for_pool(agent_name, effective_state, p["pool_group"], p["window"])
            measured_raw = win.get("measured_at") if win.get("used_pct") is not None else None
            measured_dt = parse_timestamp(measured_raw) if measured_raw else None
            if measured_dt is not None and measured_dt > created_dt:
                continue
            if obs is not None:
                obs_dt = obs.get("observed_dt")
                if measured_dt is not None and obs_dt is not None and obs_dt <= measured_dt:
                    continue
                if obs.get("credit_usd") is not None:
                    amount = max(0.0, float(obs["credit_usd"]))
                elif obs.get("outcome") == "interrupted":
                    amount = 0.0
                else:
                    amount = float(entry["reserved_credit_usd"])
            else:
                amount = float(entry["reserved_credit_usd"])
            if amount > 0.0:
                key = (p["pool_group"], p["window"])
                totals[key] = round(totals.get(key, 0.0) + amount, 6)
    return totals


def resolve_model_quota(
    agent_name,
    agent_cfg,
    effective_state,
    candidate,
    *,
    is_host=False,
    surface="cli",
    calibration_store=None,
    reserved_credit_by_pool=None,
):
    """Resolve effective 5h/7d quota windows and eligibility for a specific model."""
    model_id = str(candidate["model"])
    from .adapters import get_adapter

    try:
        adapter = get_adapter(agent_name)
        quota_pools = adapter.resolve_quota_pools(model_id, candidate, effective_state)
        quota_group, s5, w7 = quota_pools[0]
    except Exception:
        s5 = deepcopy(effective_state["session_5h"])
        w7 = deepcopy(effective_state["weekly_7d"])
        quota_group = "shared"
        quota_pools = [(quota_group, s5, w7)]

    from .calibration import (
        _canonical_pool_group_for_window,
        credit_reserve_usd,
        resolve_calibrated_quota_credit,
    )
    from .config import resolve_plan_credit_sources

    reserve = agent_cfg["host_reserve_pct"] if is_host else 0.0
    reserve_credit = credit_reserve_usd(agent_cfg, is_host=is_host)
    credit_budget, credit_calibration, unknown_ceiling_windows = resolve_calibrated_quota_credit(
        agent_name,
        agent_cfg,
        effective_state,
        quota_group,
        s5,
        w7,
        reserve_pct=reserve,
        reserve_credit_usd=reserve_credit,
        calibration_store=calibration_store,
        quota_pools=quota_pools,
        reserved_credit_by_pool=reserved_credit_by_pool,
    )
    _, preset_credit, manual_credit, _ = resolve_plan_credit_sources(
        agent_name, agent_cfg, effective_state.get("detected_plan")
    )
    reservation_pools: list[dict] = []
    seen_res_keys: set[tuple[str, str]] = set()
    for window, win_idx in (("session_5h", 1), ("weekly_7d", 2)):
        for pool_entry in quota_pools:
            p_group = pool_entry[0]
            win_dict = pool_entry[win_idx]
            canon_group, prior_c, manual_c = _canonical_pool_group_for_window(
                agent_name, p_group, window, preset_credit, manual_credit
            )
            if prior_c is None and manual_c is None:
                continue
            rkey = (canon_group, window)
            if rkey in seen_res_keys:
                continue
            seen_res_keys.add(rkey)
            reservation_pools.append(
                {
                    "pool_group": canon_group,
                    "window": window,
                    "reset_at": win_dict.get("reset_at") if isinstance(win_dict, dict) else None,
                }
            )
    rem_5h = credit_budget.get("session_5h_remaining_credit_usd") if credit_budget else None
    rem_7d = credit_budget.get("weekly_7d_remaining_credit_usd") if credit_budget else None
    # A window without a known credit ceiling keeps only its percentage limits.
    reserve_warnings = [
        f"{agent_name} quota_group '{group}' {window}: no known credit ceiling, so the "
        f"{reserve_credit:.6g} USD credit reserve is not applied (percentage limits only)"
        for group, window in (unknown_ceiling_windows if reserve_credit > 0.0 else ())
    ]

    def _result(eligible, exclusion_reason):
        res = {
            "model": model_id,
            "surface": surface,
            "quota_group": quota_group,
            "session_5h": s5,
            "weekly_7d": w7,
            "eligible": eligible,
            "exclusion_reason": exclusion_reason,
            "credit_reserve_warnings": list(reserve_warnings),
            "reservation_pools": deepcopy(reservation_pools),
        }
        if credit_budget is not None:
            res["credit_budget"] = deepcopy(credit_budget)
        if credit_calibration is not None:
            res["credit_calibration"] = deepcopy(credit_calibration)
            for key in ("monthly_cost_usd", "weekly_cost_usd", "cost_per_credit"):
                res[key] = credit_calibration[key]
        return res

    if not agent_cfg["enabled"]:
        return _result(False, "agent is disabled in configuration")

    auth_reason = _auth_failure_reason(agent_name, effective_state)
    if auth_reason:
        return _result(False, auth_reason)

    excluded_groups = agent_cfg.get("exclude_quota_groups", ())
    for grp, _, _ in quota_pools:
        if grp in excluded_groups:
            return _result(False, f"quota_group '{grp}' is excluded in configuration")

    rejection = _rejection_reason(agent_name, effective_state) or _scoped_access_rejection(
        agent_name, effective_state, candidate
    )
    if not rejection:
        for grp, p_s5, p_w7 in quota_pools:
            rejection = _window_rejection_reason(agent_name, grp, p_s5, p_w7)
            if rejection:
                break
    if rejection:
        return _result(False, rejection)

    if effective_state["rate_limited"]:
        until = effective_state["cooldown_until"]
        suffix = f" until {until}" if until else ""
        return _result(False, f"agent is rate-limited{suffix}")
    if effective_state["cooldown_until"] is not None:
        return _result(False, f"agent is in cooldown until {effective_state['cooldown_until']}")

    if credit_budget is not None and credit_budget["available_credit_usd"] <= 0.0:
        exhausted_win = "session_5h" if (rem_5h is not None and rem_5h <= 0.0) else "weekly_7d"
        held_back = credit_calibration[exhausted_win]["reserve_credit_usd"]
        reserved_amt = credit_calibration[exhausted_win].get("reserved_credit_usd", 0.0)
        parts = []
        if held_back > 0.0:
            parts.append(f"{held_back:.6g} USD reserve")
        if reserved_amt > 0.0:
            parts.append(f"{reserved_amt:.6g} USD reserved")
        after_detail = f" after {' and '.join(parts)}" if parts else ""
        return _result(False, f"{exhausted_win} credit_budget exhausted (0 USD remaining{after_detail})")

    max_session = max(0.0, agent_cfg["max_session_used_pct"] - reserve)
    max_weekly = max(0.0, agent_cfg["max_weekly_used_pct"] - reserve)

    ok_tier, reason_tier = evaluate_candidate_tier(agent_cfg, effective_state, candidate)
    if not ok_tier:
        return _result(False, reason_tier)

    for idx, (grp, p_s5, p_w7) in enumerate(quota_pools):
        prefix = "" if idx == 0 else f"overlapping quota_group '{grp}' "
        if p_s5["used_pct"] is not None and p_s5["used_pct"] >= max_session:
            return _result(
                False,
                f"{prefix}session_5h used_pct ({p_s5['used_pct']:.1f}%) >= effective limit ({max_session:.1f}%)",
            )
        if p_w7["used_pct"] is not None and p_w7["used_pct"] >= max_weekly:
            return _result(
                False,
                f"{prefix}weekly_7d used_pct ({p_w7['used_pct']:.1f}%) >= effective limit ({max_weekly:.1f}%)",
            )

    return _result(True, None)


def _extract_window_dict(obj, where):
    table(obj, where)
    used = obj.get("used_pct", obj.get("used_percent", obj.get("usedPercent", obj.get("percent"))))
    if used is None and "remaining_pct" in obj and obj["remaining_pct"] is not None:
        used = 100.0 - percentage(obj["remaining_pct"], f"{where}.remaining_pct")
    elif used is None and "remaining_percent" in obj and obj["remaining_percent"] is not None:
        used = 100.0 - percentage(obj["remaining_percent"], f"{where}.remaining_percent")
    elif used is None and "remaining_fraction" in obj and obj["remaining_fraction"] is not None:
        rem_frac = percentage(float(obj["remaining_fraction"]) * 100.0, f"{where}.remaining_fraction")
        used = round(100.0 - rem_frac, 2)
    elif used is None and "utilization" in obj and obj["utilization"] is not None:
        util = percentage(obj["utilization"], f"{where}.utilization")
        if util == 0:
            used = 0.0
        elif util < 1.0 and isinstance(obj["utilization"], float):
            used = util * 100.0
        elif util == 1.0 and "resetsAt" in obj:
            used = 100.0
        else:
            used = util
    elif used is not None:
        used = percentage(used, f"{where}.used_pct")
    reset_raw = obj.get("reset_at", obj.get("resets_at", obj.get("resetsAt", obj.get("reset_time"))))
    reset_at = format_timestamp(parse_timestamp(reset_raw, f"{where}.reset_at")) if reset_raw is not None else None
    result = {"used_pct": used, "reset_at": reset_at}
    for key in ("observed_at", "measured_at"):
        if obj.get(key) is not None:
            result[key] = format_timestamp(parse_timestamp(obj[key], f"{where}.{key}"))
    return result


def _apply_rate_limits_dict(rl_obj, patch, where="rate_limits"):
    table(rl_obj, where)
    changed = False
    raw_plan_type = rl_obj.get("planType", rl_obj.get("plan_type"))
    if isinstance(raw_plan_type, str) and raw_plan_type.strip():
        from .config import normalize_plan_slug
        canonical = normalize_plan_slug("codex", raw_plan_type)
        if canonical is not None:
            patch["detected_plan"] = canonical
    for slot in ("primary", "secondary", "session_5h", "weekly_7d"):
        if slot in rl_obj and isinstance(rl_obj[slot], dict):
            sub = rl_obj[slot]
            win = _extract_window_dict(sub, f"{where}.{slot}")
            win_mins = sub.get("window_minutes", sub.get("windowDurationMins"))
            if win_mins is not None:
                win_mins = number(win_mins, f"{where}.{slot}.window_minutes", positive=True)
            if slot == "session_5h" or (slot == "primary" and (win_mins is None or win_mins <= 360)):
                patch["session_5h"] = win
                changed = True
            elif slot == "weekly_7d" or slot == "secondary" or (win_mins is not None and win_mins > 360):
                patch["weekly_7d"] = win
                changed = True
    if "rate_limited" in rl_obj and rl_obj["rate_limited"] is not None:
        patch["rate_limited"] = boolean(rl_obj["rate_limited"], f"{where}.rate_limited")
        changed = True
    if "cooldown_until" in rl_obj and rl_obj["cooldown_until"] is not None:
        patch["cooldown_until"] = format_timestamp(
            parse_timestamp(rl_obj["cooldown_until"], f"{where}.cooldown_until")
        )
        changed = True
    if "tiers" in rl_obj and isinstance(rl_obj["tiers"], dict):
        tiers = patch.setdefault("tiers", {})
        for tier_name, tier_obj in rl_obj["tiers"].items():
            text(tier_name, f"{where}.tiers.{tier_name}", label=True, max_length=64)
            tiers[tier_name] = _extract_window_dict(tier_obj, f"{where}.tiers.{tier_name}")
            changed = True
    return changed


def _apply_agy_usage_command(cmd_data: dict, patch: dict, *, successful: bool = False) -> bool:
    """Normalize an Antigravity `/usage` report.

    A successful report in which every group and bucket carries quota evidence
    describes the plan's whole quota-window set; it is marked complete so that
    applying it replaces, rather than merges into, the stored windows.
    """
    groups = cmd_data.get("groups")
    if not isinstance(groups, list):
        return False
    changed = False
    complete = successful and bool(groups)
    tiers = patch.setdefault("tiers", {})
    for grp in groups:
        if not isinstance(grp, dict):
            complete = False
            continue
        grp_name = str(grp.get("name") or "").strip().lower()
        buckets = grp.get("buckets")
        if not isinstance(buckets, list) or not buckets:
            complete = False
            continue
        for bkt in buckets:
            if not isinstance(bkt, dict):
                complete = False
                continue
            bid = str(bkt.get("id") or "").strip().lower()
            window = str(bkt.get("window") or "").strip().lower()
            win = _extract_window_dict(bkt, f"agy.command.data.{bid or 'bucket'}")
            if win["used_pct"] is None and win["reset_at"] is None:
                complete = False
                continue
            changed = True
            if bid:
                tiers[bid] = win
            is_3p = bid.startswith("3p-") or "claude" in grp_name or "gpt" in grp_name
            is_argon = "argon" in bid or "argon" in grp_name
            if is_3p:
                if window in ("5h", "session_5h", "five_hour"):
                    tiers["3p-5h"] = win
                elif window in ("weekly", "7d", "weekly_7d"):
                    tiers["3p-weekly"] = win
            elif is_argon:
                tiers["argon"] = win
                if window in ("5h", "session_5h", "five_hour"):
                    tiers["argon-5h"] = win
                else:
                    tiers["argon-weekly"] = win
            elif "gemini" in grp_name or bid.startswith("gemini-"):
                if window in ("5h", "session_5h", "five_hour"):
                    patch["session_5h"] = win
                    tiers["gemini-5h"] = win
                elif window in ("weekly", "7d", "weekly_7d"):
                    patch["weekly_7d"] = win
                    tiers["gemini-weekly"] = win
            else:
                # Generic dynamic group token extraction for any future model groups
                for tok in re.split(r"[-_.\s]+", grp_name):
                    if len(tok) >= 3 and tok not in ("models", "model", "limit", "weekly", "hour", "five", "and"):
                        if window in ("5h", "session_5h", "five_hour"):
                            tiers[f"{tok}-5h"] = win
                        else:
                            tiers[f"{tok}-weekly"] = win
                            tiers[tok] = win
    if changed:
        patch.setdefault("rate_limited", False)
        if complete:
            patch[COMPLETE_REPORT] = True
    return changed


def _claude_scope(scope):
    if scope is None or scope == "":
        return None
    if isinstance(scope, dict):
        model = scope.get("model")
        if isinstance(model, dict):
            model = model.get("display_name") or model.get("id") or model.get("name")
        surface = scope.get("surface")
        if isinstance(surface, dict):
            surface = surface.get("display_name") or surface.get("id") or surface.get("name")
        if not isinstance(model, str) or not model.strip():
            raise AgRouteError("claude scope: missing recognized model name")
        if surface is not None and not isinstance(surface, str):
            raise AgRouteError("claude scope: unrecognized surface")
        scope = model + ("-" + surface if surface and surface.strip() else "")
    if not isinstance(scope, str):
        raise AgRouteError("claude scope: expected a model name or structured scope")
    slug = re.sub(r"[^a-z0-9_-]+", "-", scope.lower()).strip("-_")
    text(slug, "claude scope", label=True, max_length=57)
    return slug


def _claude_window_type(kind, group=""):
    kind, group = str(kind or "").lower(), str(group or "").lower()
    if kind in ("session", "five_hour", "session_5h", "5h") or group == "session":
        return "session_5h", None
    if kind in ("weekly_all", "weekly", "seven_day", "weekly_7d", "7d") or group == "weekly":
        return "weekly_7d", None
    for prefix in ("weekly_", "seven_day_", "five_hour_"):
        if kind.startswith(prefix):
            return ("session_5h" if prefix == "five_hour_" else "weekly_7d"), _claude_scope(kind[len(prefix):])
    raise AgRouteError(f"claude limit: unrecognized window {kind!r}")


def _claude_put(patch, kind, scope, win, group=""):
    slot, inferred_scope = _claude_window_type(kind, group)
    scope_key = _claude_scope(scope) or inferred_scope
    if scope_key:
        suffix = "5h" if slot == "session_5h" else "weekly"
        tiers = patch.setdefault("tiers", {})
        # Keep the legacy bare tier alias while reconciling availability by window.
        for key in (scope_key, f"{scope_key}-{suffix}"):
            tiers[key] = _merge_window(tiers.get(key, _empty_window()), win)
    else:
        patch[slot] = _merge_window(patch.get(slot, _empty_window()), win)


def _claude_evidence(item, timestamp=None, event_id=None):
    win = _extract_window_dict(item, "claude limit")
    status = item.get("status")
    if status in ("rejected", "rate_limited", "exhausted", 429) or item.get("rate_limited") is True:
        win["status"] = "rejected"
    elif status in ("allowed", "accepted"):
        win["status"] = "allowed"
    elif win["used_pct"] is not None:
        win["status"] = "reported"
    if timestamp is not None:
        win["observed_at"] = format_timestamp(parse_timestamp(timestamp, "claude event timestamp"))
    if event_id:
        win["event_id"] = event_id
    return win


def _warn(patch, exc):
    patch.setdefault("warnings", []).append(str(exc))


def _apply_claude_usage_report(report, patch, timestamp=None):
    rl = report.get("rate_limits")
    if not isinstance(rl, dict) or not isinstance(rl.get("limits"), list):
        return False
    changed = False
    for index, item in enumerate(rl["limits"]):
        try:
            table(item, f"claude limits[{index}]")
            win = _claude_evidence(item, timestamp)
            if "status" not in win:
                raise AgRouteError(f"claude limits[{index}]: no recognized availability or percentage")
            _claude_put(patch, item.get("kind"), item.get("scope"), win, item.get("group"))
            changed = True
        except (AgRouteError, TypeError, ValueError) as exc:
            _warn(patch, f"claude limits[{index}] skipped: {exc}")
    return changed


def _apply_claude_event(obj, patch):
    rli = obj.get("rate_limit_info", obj.get("rateLimitInfo", obj))
    if not isinstance(rli, dict):
        _warn(patch, "claude rate-limit event skipped: unrecognized payload")
        return False
    timestamp = obj.get("timestamp", rli.get("timestamp"))
    event_id = hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    changed = False
    entries = []
    kind = rli.get("rate_limit_type", rli.get("rateLimitType", rli.get("window")))
    if kind is not None:
        entries.append((kind, rli))
    elif rli.get("status") in ("rejected", "rate_limited", "exhausted", "allowed", "accepted", 429):
        # Credit/entitlement rejection without a quota window is agent-wide.
        try:
            win = _claude_evidence(rli, timestamp, event_id)
            scope = _claude_scope(rli.get("scope"))
            if scope:
                scoped = patch.setdefault("scope_availability", {})
                scoped[scope] = _merge_window(scoped.get(scope, _empty_window()), win)
            else:
                patch["availability"] = _merge_window(patch.get("availability", _empty_window()), win)
            changed = True
        except (AgRouteError, TypeError, ValueError) as exc:
            _warn(patch, f"claude rate-limit event skipped: {exc}")
    unified = rli.get("unifiedWindows")
    if isinstance(unified, dict):
        entries.extend(unified.items())
    for entry_kind, item in entries:
        try:
            table(item, "claude rate-limit window")
            win = _claude_evidence(item, timestamp, event_id)
            if "status" not in win:
                continue
            _claude_put(patch, entry_kind, item.get("scope", rli.get("scope")), win)
            changed = True
        except (AgRouteError, TypeError, ValueError) as exc:
            _warn(patch, f"claude rate-limit window skipped: {exc}")
    return changed


def _apply_agy_status_line(obj: dict, patch: dict) -> bool:
    """Normalize an Antigravity `statusLine` JSON payload (`plan_tier` and/or `quota`)."""
    raw_plan = obj.get("plan_tier")
    if isinstance(raw_plan, str) and raw_plan.strip():
        from .config import normalize_plan_slug
        canonical = normalize_plan_slug("agy", raw_plan)
        if canonical is not None:
            patch["detected_plan"] = canonical
    quota_obj = obj.get("quota")
    if not isinstance(quota_obj, dict) or not quota_obj:
        return False
    changed = False
    complete = True
    tiers = patch.setdefault("tiers", {})
    for raw_bid, bkt in quota_obj.items():
        if not isinstance(bkt, dict):
            complete = False
            continue
        bid = str(raw_bid or "").strip().lower()
        if not bid:
            complete = False
            continue
        if "remaining_fraction" in bkt and bkt["remaining_fraction"] is not None:
            frac = float(bkt["remaining_fraction"])
            percentage(frac * 100.0, f"agy.quota.{bid}.remaining_fraction")
            used_pct = round((1.0 - frac) * 100.0, 6)
            reset_raw = bkt.get("reset_at", bkt.get("resets_at", bkt.get("resetsAt", bkt.get("reset_time"))))
            reset_at = (
                format_timestamp(parse_timestamp(reset_raw, f"agy.quota.{bid}.reset_time"))
                if reset_raw is not None
                else None
            )
            win = {"used_pct": used_pct, "reset_at": reset_at}
            for key in ("observed_at", "measured_at"):
                if bkt.get(key) is not None:
                    win[key] = format_timestamp(parse_timestamp(bkt[key], f"agy.quota.{bid}.{key}"))
        else:
            win = _extract_window_dict(bkt, f"agy.quota.{bid}")
        if win["used_pct"] is None and win["reset_at"] is None:
            complete = False
            continue
        changed = True
        tiers[bid] = win
        if bid == "gemini-5h":
            patch["session_5h"] = win
        elif bid == "gemini-weekly":
            patch["weekly_7d"] = win
        elif "argon" in bid:
            tiers["argon"] = win
            if bid.endswith("-5h"):
                tiers["argon-5h"] = win
            else:
                tiers["argon-weekly"] = win
    if changed:
        patch.setdefault("rate_limited", False)
        if complete:
            patch[COMPLETE_REPORT] = True
    return changed


def _extract_claude_detected_plan(obj: dict) -> str | None:
    """Normalize `claude auth status` `planTier` / `subscriptionType` to canonical `claude` plan."""
    from .config import normalize_plan_slug

    plan_tier = obj.get("planTier")
    if isinstance(plan_tier, str) and plan_tier.strip():
        canonical = normalize_plan_slug("claude", plan_tier)
        if canonical is not None:
            return canonical
    sub_type = obj.get("subscriptionType")
    if isinstance(sub_type, str) and sub_type.strip():
        canonical = normalize_plan_slug("claude", sub_type)
        if canonical is not None:
            return canonical
    return None


_AUTH_ERROR_PHRASES = (
    "failed to authenticate",
    "authentication_failed",
    "authentication_error",
    "login reached its time limit",
    "oauth token has expired",
    "not logged in",
    "claude auth login",
    "please run /login",
    "invalid bearer token",
    "invalid api key",
)

_TEXT_EXPIRED_PHRASES = (
    "login: expired",
    "login:expired",
    "expired — log in again",
    "expired - log in again",
    "login reached its time limit",
    "oauth token has expired",
)

_TEXT_AUTH_PHRASES = (
    "not logged in",
    "notloggedin",
    "failed to authenticate",
    "authentication_failed",
    "authentication_error",
    "claude auth login",
    "please run /login",
)


def _extract_claude_error_text(obj: dict) -> str:
    parts: list[str] = []
    for key in ("result", "error", "reason", "status_message", "detail"):
        val = obj.get(key)
        if isinstance(val, str) and val:
            parts.append(val)
    msg = obj.get("message")
    if isinstance(msg, str) and msg:
        parts.append(msg)
    elif isinstance(msg, dict):
        if isinstance(msg.get("error"), str) and msg["error"]:
            parts.append(msg["error"])
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
    return "\n".join(parts)


def _make_claude_auth_patch(raw_text: str) -> dict:
    lower = raw_text.lower()
    if any(tok in lower for tok in ("expired", "time limit", "again")):
        return {
            "status": "expired",
            "error": "Claude login expired; run 'claude auth login' to sign in again",
        }
    return {
        "status": "logged_out",
        "error": "Claude is not logged in; run 'claude auth login' to authenticate",
    }


def _claude_auth_status_from_obj(obj: dict, *, raw_text: str = "", now_dt=None) -> dict | None:
    if obj.get("loggedIn") is False or obj.get("authMethod") == "none":
        expired = bool(obj.get("loginEnded"))
        if not expired and obj.get("loginExpiresAt") is not None:
            if now_dt is not None:
                try:
                    expired = now_dt >= parse_timestamp(obj["loginExpiresAt"], "loginExpiresAt")
                except AgRouteError:
                    expired = True
            else:
                expired = True
        if not expired and "expired" in (raw_text or json.dumps(obj)).lower():
            expired = True
        if expired:
            return {
                "status": "expired",
                "error": "Claude login expired; run 'claude auth login' to sign in again",
            }
        return {
            "status": "logged_out",
            "error": "Claude is not logged in; run 'claude auth login' to authenticate",
        }
    if obj.get("loggedIn") is True and obj.get("loginExpiresAt") is not None and now_dt is not None:
        try:
            if now_dt >= parse_timestamp(obj["loginExpiresAt"], "loginExpiresAt"):
                return {
                    "status": "expired",
                    "error": "Claude login expired; run 'claude auth login' to sign in again",
                }
        except AgRouteError:
            pass
    return None


def _parse_json_events(raw_text, patch, *, now_dt=None):
    lines = [line.strip() for line in raw_text.splitlines() if line.strip()]
    if not lines:
        return False
    parsed_objects = []
    try:
        top = json.loads(raw_text)
        if isinstance(top, dict):
            parsed_objects = [top]
        elif isinstance(top, list) and all(isinstance(item, dict) for item in top):
            parsed_objects = top
    except ValueError:
        for line in lines:
            brace = line.find("{")
            if brace < 0:
                continue
            candidate = line[brace:].strip()
            try:
                obj = json.loads(candidate)
            except ValueError:
                continue
            if isinstance(obj, dict):
                parsed_objects.append(obj)
    if not parsed_objects:
        return False

    changed = False
    saw_api_key_none = False
    served_by_anthropic_false = False
    saw_usage_local_command = False
    saw_estimated_cost = False
    saw_usage_report = False
    last_observed = None

    for obj in parsed_objects:
        if isinstance(obj.get("usage_report"), dict):
            saw_usage_report = True
        if obj.get("apiKeySource") == "none":
            saw_api_key_none = True
        if obj.get("served_by_anthropic") is False:
            served_by_anthropic_false = True
        lcr = obj.get("local_command_run")
        if (
            obj.get("local_command") == "usage"
            or (isinstance(lcr, dict) and lcr.get("command") == "usage")
            or (obj.get("subtype") == "local_command_run" and obj.get("command") == "usage")
        ):
            saw_usage_local_command = True
        if isinstance(obj.get("result"), str) and "estimated cost:" in obj["result"].lower():
            saw_estimated_cost = True

        event_patch = {}
        recognized = _parse_json_object(obj, event_patch, now_dt=now_dt)
        if event_patch.get("detected_plan"):
            patch["detected_plan"] = event_patch["detected_plan"]
        if event_patch.get("logged_in") is True and "auth" not in event_patch:
            patch["logged_in"] = True
        changed = recognized or changed
        observed = obj.get("timestamp", obj.get("updated_at"))
        if observed is not None:
            observed = format_timestamp(parse_timestamp(observed, "usage event timestamp"))
            last_observed = observed
            if recognized:
                for _, win in _patch_windows(event_patch):
                    win.setdefault("observed_at", observed)
                    if win.get("used_pct") is not None:
                        win.setdefault("measured_at", win["observed_at"])
        for key, win in _patch_windows(event_patch):
            _put_window(patch, key, _merge_window(_get_window(patch, key), win))
        if "auth" in event_patch:
            auth_entry = deepcopy(event_patch["auth"])
            if observed is not None:
                auth_entry.setdefault("observed_at", observed)
            prev_auth = patch.get("auth")
            if (
                not isinstance(prev_auth, dict)
                or not prev_auth.get("observed_at")
                or (
                    auth_entry.get("observed_at")
                    and parse_timestamp(auth_entry["observed_at"]) >= parse_timestamp(prev_auth["observed_at"])
                )
            ):
                patch["auth"] = auth_entry
            patch.pop("logged_in", None)
        elif recognized and isinstance(patch.get("auth"), dict):
            has_valid_evidence = event_patch.get("rate_limited") is False or any(
                win.get("used_pct") is not None or win.get("status") == "allowed"
                for _, win in _patch_windows(event_patch)
            )
            if has_valid_evidence and (
                observed is None
                or not patch["auth"].get("observed_at")
                or parse_timestamp(observed) >= parse_timestamp(patch["auth"]["observed_at"])
            ):
                patch.pop("auth", None)
        if "rate_limited" in event_patch or "cooldown_until" in event_patch:
            # Only the object supplying global availability owns its clock.
            # A later unrelated result cannot renew the earlier observation.
            for key in ("rate_limited", "cooldown_until"):
                if key in event_patch:
                    patch[key] = event_patch[key]
            if observed is not None:
                patch["updated_at"] = observed
            else:
                patch.pop("updated_at", None)
        if event_patch.get("warnings"):
            patch.setdefault("warnings", []).extend(event_patch["warnings"])
        if event_patch.get(COMPLETE_REPORT):
            patch[COMPLETE_REPORT] = True

    if (
        not changed
        and not saw_usage_report
        and not served_by_anthropic_false
        and saw_api_key_none
        and saw_usage_local_command
        and saw_estimated_cost
    ):
        auth_entry = {
            "status": "logged_out",
            "error": "Claude is not logged in (or login expired); run 'claude auth login' to authenticate",
        }
        if last_observed is not None:
            auth_entry["observed_at"] = last_observed
        patch["auth"] = auth_entry
        changed = True
    return changed


def _parse_json_object(obj, patch, *, now_dt=None):
    """Normalize one external event before combining independent observations."""
    changed = False
    effective_now = now_dt
    if effective_now is None and obj.get("timestamp") is not None:
        try:
            effective_now = parse_timestamp(obj["timestamp"], "usage event timestamp")
        except AgRouteError:
            effective_now = None
    auth_obj_patch = _claude_auth_status_from_obj(obj, now_dt=effective_now)
    if auth_obj_patch is not None:
        patch["auth"] = auth_obj_patch
        changed = True
    elif obj.get("loggedIn") is True:
        patch["logged_in"] = True

    if _apply_rate_limits_dict(obj, patch, "usage object"):
        changed = True
    if "rate_limits" in obj and isinstance(obj["rate_limits"], dict):
        if _apply_rate_limits_dict(obj["rate_limits"], patch, "rate_limits"):
            changed = True
    if "rateLimits" in obj and isinstance(obj["rateLimits"], dict):
        if _apply_rate_limits_dict(obj["rateLimits"], patch, "rateLimits"):
            changed = True
    rpc_res = obj.get("result")
    if isinstance(rpc_res, dict):
        if "rateLimits" in rpc_res and isinstance(rpc_res["rateLimits"], dict):
            if _apply_rate_limits_dict(rpc_res["rateLimits"], patch, "result.rateLimits"):
                changed = True
        by_id = rpc_res.get("rateLimitsByLimitId")
        if isinstance(by_id, dict):
            tiers = patch.setdefault("tiers", {})
            for lid, lobj in by_id.items():
                if not isinstance(lobj, dict):
                    continue
                if lid == "codex":
                    if _apply_rate_limits_dict(lobj, patch, "result.rateLimitsByLimitId.codex"):
                        changed = True
                    continue
                sub_patch: dict = {}
                if _apply_rate_limits_dict(lobj, sub_patch, f"result.rateLimitsByLimitId.{lid}"):
                    slug = str(lobj.get("normalModelSlug") or lobj.get("limitName") or lid).strip().lower()
                    if "session_5h" in sub_patch:
                        tiers[f"{slug}-5h"] = sub_patch["session_5h"]
                        tiers.setdefault(slug, sub_patch["session_5h"])
                    if "weekly_7d" in sub_patch:
                        tiers[f"{slug}-weekly"] = sub_patch["weekly_7d"]
                        tiers[slug] = sub_patch["weekly_7d"]
                    changed = True
    cmd_obj = obj.get("command")
    if isinstance(cmd_obj, dict) and cmd_obj.get("name") == "usage" and isinstance(cmd_obj.get("data"), dict):
        if _apply_agy_usage_command(cmd_obj["data"], patch, successful=obj.get("status") == "SUCCESS"):
            changed = True
    if _apply_agy_status_line(obj, patch):
        changed = True
    claude_plan = _extract_claude_detected_plan(obj)
    if claude_plan is not None:
        patch["detected_plan"] = claude_plan
    usage_rep = obj.get("usage_report")
    if isinstance(usage_rep, dict):
        if _apply_claude_usage_report(usage_rep, patch, obj.get("timestamp")):
            changed = True
    payload = obj.get("payload")
    if isinstance(payload, dict):
        if "rate_limits" in payload and isinstance(payload["rate_limits"], dict):
            if _apply_rate_limits_dict(payload["rate_limits"], patch, "payload.rate_limits"):
                changed = True
        info = payload.get("info")
        if isinstance(info, dict) and isinstance(info.get("rate_limits"), dict):
            if _apply_rate_limits_dict(info["rate_limits"], patch, "payload.info.rate_limits"):
                changed = True
    if obj.get("type") == "rate_limit_event" or "rate_limit_info" in obj or "rateLimitInfo" in obj:
        if _apply_claude_event(obj, patch):
            changed = True
    if obj.get("type") == "assistant" and obj.get("error") in (
        "authentication_failed",
        "authentication_error",
        "unauthorized",
    ):
        patch["auth"] = _make_claude_auth_patch(_extract_claude_error_text(obj))
        changed = True
    if obj.get("type") == "result" and obj.get("is_error") is True:
        err_text = _extract_claude_error_text(obj).lower()
        if (
            obj.get("api_error_status") == 401
            or obj.get("error") in ("authentication_failed", "authentication_error", "unauthorized")
            or any(phrase in err_text for phrase in _AUTH_ERROR_PHRASES)
        ):
            patch["auth"] = _make_claude_auth_patch(err_text)
            changed = True
        elif (
            obj.get("api_error_status") in (403, 429)
            or obj.get("terminal_reason") in ("api_error", "rate_limit")
            or any(k in err_text for k in ("rate limit", "quota", "subscription", "usage limit", "hit your weekly limit"))
        ):
            patch["rate_limited"] = True
            changed = True
    if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
        res_obj = obj["result"]
        if res_obj.get("status") == "RATE_LIMITED":
            patch["rate_limited"] = True
            changed = True
    return changed


_PCT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*(used|remaining)?", re.IGNORECASE)
_RESET_RE = re.compile(
    r"resets?\s+(?:at\s+)?([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+-]+Z?|\d{10}(?:\.\d+)?)",
    re.IGNORECASE,
)
_CLAUDE_LIMIT_RE = re.compile(r"Claude AI usage limit reached\|(\d+)", re.IGNORECASE)
_HIT_LIMIT_RE = re.compile(r"\bhit\s+your\b.{0,40}\blimit\b", re.IGNORECASE)
_TIER_LINE_RE = re.compile(
    r"(?:tier\s+([a-z0-9._-]+)|(?:current\s+week|weekly|limit|quota)\s*\(\s*([a-z0-9._-]+)\s*\)|^\s*([a-z0-9._-]+)\s*:\s*\d)",
    re.IGNORECASE,
)


def _parse_text_usage(raw_text, patch):
    changed = False
    claude_match = _CLAUDE_LIMIT_RE.search(raw_text)
    if claude_match:
        epoch = int(claude_match.group(1))
        iso = format_timestamp(parse_timestamp(epoch, "claude usage limit epoch"))
        patch["rate_limited"] = True
        patch["cooldown_until"] = iso
        changed = True

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()
        if any(phrase in lower for phrase in _TEXT_EXPIRED_PHRASES):
            patch["auth"] = {
                "status": "expired",
                "error": "Claude login expired; run 'claude auth login' to sign in again",
            }
            changed = True
            continue
        if any(phrase in lower for phrase in _TEXT_AUTH_PHRASES):
            if any(tok in lower for tok in ("expired", "time limit", "again")):
                patch["auth"] = {
                    "status": "expired",
                    "error": "Claude login expired; run 'claude auth login' to sign in again",
                }
            else:
                patch.setdefault(
                    "auth",
                    {
                        "status": "logged_out",
                        "error": "Claude is not logged in; run 'claude auth login' to authenticate",
                    },
                )
            changed = True
            continue
        reset_match = _RESET_RE.search(line)
        reset_iso = (
            format_timestamp(parse_timestamp(reset_match.group(1), "text reset_at"))
            if reset_match
            else None
        )
        if (
            any(
                phrase in lower
                for phrase in (
                    "rate limit reached",
                    "rate limit exceeded",
                    "quota exceeded",
                    "usage limit reached",
                    "rate_limit_error",
                    "too many requests",
                    "http 429",
                )
            )
            or _HIT_LIMIT_RE.search(lower)
        ):
            patch["rate_limited"] = True
            if reset_iso is not None:
                patch["cooldown_until"] = reset_iso
            changed = True

        pct_match = _PCT_RE.search(line)
        if not pct_match:
            continue
        val = percentage(float(pct_match.group(1)), "text percentage")
        direction = (pct_match.group(2) or "used").lower()
        used_pct = 100.0 - val if direction == "remaining" else val
        win = {"used_pct": used_pct, "reset_at": reset_iso}

        # Check model tier lines first (e.g. "Current week (Opus): 80% used", "tier pro: 75% used", or "pro: 97% used")
        tier_match = _TIER_LINE_RE.search(lower)
        if tier_match:
            tier_token = (tier_match.group(1) or tier_match.group(2) or tier_match.group(3) or "").strip()
            if tier_token and tier_token not in (
                "all",
                "session",
                "session_5h",
                "5h",
                "5-hour",
                "7d",
                "7-day",
                "weekly",
                "weekly_7d",
                "primary",
                "secondary",
                "limit",
                "quota",
            ):
                tiers = patch.setdefault("tiers", {})
                tiers[tier_token] = win
                changed = True
                continue

        if any(tok in lower for tok in ("5h", "5-hour", "current session", "session_5h", "session limit", "primary")):
            patch["session_5h"] = win
            changed = True
        elif any(tok in lower for tok in ("7d", "7-day", "weekly", "current week", "weekly_7d", "secondary")):
            patch["weekly_7d"] = win
            changed = True
    return changed


def parse_native_usage(agent, raw_text, *, tier=None, now=None):
    agent_id(agent, "agent")
    if tier is not None:
        tier = text(tier, "tier", label=True, max_length=64)
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise AgRouteError("usage ingest: input text is empty")
    now_dt = _now_utc(now) if now is not None else None
    patch = {}
    recognized = _parse_json_events(raw_text, patch, now_dt=now_dt)
    if not recognized:
        recognized = _parse_text_usage(raw_text, patch)
    if not recognized:
        raise AgRouteError(f"usage ingest: no recognizable native usage or rate-limit data found for '{agent}'")
    if tier is not None and not patch.get("auth"):
        rejected_reset = patch.get("cooldown_until")
        scoped_rejected = patch.get("rate_limited") is True
        if not scoped_rejected:
            for slot in ("availability", "weekly_7d", "session_5h"):
                win = patch.get(slot)
                if isinstance(win, dict) and win.get("status") == "rejected":
                    scoped_rejected = True
                    rejected_reset = rejected_reset or win.get("reset_at")
                    if win.get("used_pct") is None:
                        patch.pop(slot, None)
        if scoped_rejected:
            patch.pop("rate_limited", None)
            patch.pop("cooldown_until", None)
            tiers = patch.setdefault("tiers", {})
            tier_win = dict(tiers.get(tier, _empty_window()))
            tier_win["status"] = "rejected"
            tier_win["reset_at"] = rejected_reset or tier_win.get("reset_at")
            tiers[tier] = tier_win
    elif agent == "claude" and patch.get("rate_limited") is True:
        slot = "weekly_7d" if "weekly limit" in raw_text.lower() else "availability"
        win = patch.get(slot, _empty_window())
        win["status"] = "rejected"
        win["reset_at"] = patch.get("cooldown_until") or win["reset_at"]
        patch[slot] = win
        patch.pop("rate_limited", None)
        patch.pop("cooldown_until", None)
    return patch


def apply_usage_patch(config, agent, patch, *, source="ingest", now=None):
    agent_id(agent, "agent")
    now_dt = _now_utc(now)
    with _usage_locked(config):
        store = read_store(config)
        # Retain expired raw scopes as refresh obligations. Evaluation alone
        # clears them; a partial report must not forget an unrefreshed scope.
        current = deepcopy(store["agents"][agent])
        # Freeze genuine legacy clocks before a partial report advances the
        # snapshot timestamp. Old sibling scopes must keep their original age.
        for _, win in _patch_windows(current):
            if _window_has_evidence(win) and current["updated_at"]:
                win.setdefault("observed_at", current["updated_at"])
                if win.get("used_pct") is not None:
                    win.setdefault("measured_at", win["observed_at"])
        if source == "probe":
            # Successful current provider observation can retire already-ended
            # scopes. Unexpired stale scopes still require matching evidence.
            current = effective_agent_state(current, now_dt, project_rejections=False)
        if current["cooldown_until"] and now_dt >= parse_timestamp(current["cooldown_until"]):
            current["rate_limited"], current["cooldown_until"] = False, None
        if patch.get(COMPLETE_REPORT) and source in ("ingest", "probe"):
            # A complete provider report is the agent's whole current quota-window
            # set: windows it omits (e.g. a previous plan's tiers) no longer exist.
            current["tiers"] = {}
            for key in ("session_5h", "weekly_7d"):
                if key not in patch:
                    current[key] = _empty_window()
        for key, win in _patch_windows(patch):
            incoming = _validate_window(win, key)
            if source in ("ingest", "probe", "set"):
                if incoming.get("used_pct") is not None:
                    if source != "set":
                        incoming.setdefault("status", "reported")
                incoming.setdefault("observed_at", format_timestamp(now_dt))
                if incoming.get("used_pct") is not None:
                    incoming.setdefault("measured_at", incoming["observed_at"])
            _put_window(current, key, _merge_window(_get_window(current, key), incoming))
        if "rate_limited" in patch and patch["rate_limited"] is not None:
            current["rate_limited"] = boolean(patch["rate_limited"], "rate_limited")
            if not current["rate_limited"] and "cooldown_until" not in patch:
                current["cooldown_until"] = None
        if "cooldown_until" in patch:
            cd = patch["cooldown_until"]
            current["cooldown_until"] = format_timestamp(parse_timestamp(cd, "cooldown_until")) if cd is not None else None
            if cd is not None and "rate_limited" not in patch:
                current["rate_limited"] = True
        if patch.get("detected_plan"):
            from .config import normalize_plan_slug
            canonical_plan = normalize_plan_slug(agent, patch["detected_plan"])
            if canonical_plan is not None:
                current["detected_plan"] = canonical_plan
        if isinstance(patch.get("auth"), dict) and patch["auth"].get("status"):
            prev_auth = current.get("auth") or {}
            superseded = list(prev_auth.get("superseded_events", []))
            if prev_auth.get("event_id") and prev_auth["event_id"] not in superseded:
                superseded.append(prev_auth["event_id"])
            incoming_auth = dict(patch["auth"])
            incoming_auth.setdefault("observed_at", format_timestamp(now_dt))
            if superseded:
                incoming_auth["superseded_events"] = list(
                    dict.fromkeys(superseded + incoming_auth.get("superseded_events", []))
                )[-256:]
            current["auth"] = _validate_auth(incoming_auth, "auth")
        else:
            has_valid_counters_or_allowed = any(
                win.get("used_pct") is not None or win.get("status") == "allowed"
                for _, win in _patch_windows(patch)
            )
            should_clear_auth = (
                source == "probe"
                or (
                    source in ("ingest", "set")
                    and (
                        has_valid_counters_or_allowed
                        or patch.get("rate_limited") is False
                        or _current_probe_evidence(patch, config.agents[agent], now_dt)
                    )
                )
            )
            if should_clear_auth and source == "ingest" and (current.get("auth") or {}).get("observed_at"):
                patch_times = [
                    win["observed_at"] for _, win in _patch_windows(patch) if win.get("observed_at")
                ]
                if patch.get("updated_at"):
                    patch_times.append(patch["updated_at"])
                if patch_times:
                    newest_patch_ts = max(patch_times, key=parse_timestamp)
                    if parse_timestamp(newest_patch_ts) < parse_timestamp(current["auth"]["observed_at"]):
                        should_clear_auth = False
            if should_clear_auth:
                prev_auth = current.get("auth") or {}
                superseded = list(prev_auth.get("superseded_events", []))
                if prev_auth.get("event_id"):
                    superseded.append(prev_auth["event_id"])
                superseded.extend(patch.get("_superseded_auth_events", []))
                superseded = list(dict.fromkeys(superseded))[-256:]
                current["auth"] = {"superseded_events": superseded} if superseded else {}
        has_quota_update = bool(list(_patch_windows(patch))) or "rate_limited" in patch or "cooldown_until" in patch
        # Snapshot time describes quota evidence; failures never refresh it.
        if source in ("ingest", "probe", "set"):
            if has_quota_update or "auth" not in patch:
                times = [win["observed_at"] for _, win in _patch_windows(current) if win.get("observed_at")]
                if current["updated_at"]:
                    times.append(current["updated_at"])
                if "rate_limited" in patch or "cooldown_until" in patch:
                    times.append(patch.get("updated_at") or format_timestamp(now_dt))
                current["updated_at"] = max(times, key=parse_timestamp) if times else format_timestamp(now_dt)
        else:
            current["updated_at"] = format_timestamp(now_dt)
        if has_quota_update or "auth" not in patch or current["source"] is None:
            current["source"] = source
        if source == "probe":
            current["probe"].update(attempted_at=format_timestamp(now_dt), succeeded_at=format_timestamp(now_dt),
                                    warnings=patch.get("warnings", []))
            current["probe"].pop("error", None)
            current["probe"].pop("failed_at", None)
        store["agents"][agent] = current
        write_store(config, store)
        if source in ("probe", "ingest", "set"):
            _release_reservations_for_patch(config, agent, patch)
            from .calibration import record_usage_calibration_snapshot

            record_usage_calibration_snapshot(config, agent, current, now_dt)
        if source in ("probe", "ingest"):
            _write_shared_probe_cache(agent, current)
        return effective_agent_state(current, now_dt)


def clear_usage(config, agent=None):
    if agent is not None:
        agent_id(agent, "agent")
    with _usage_locked(config):
        store = _read_raw_store(config)
        targets = [agent] if agent is not None else list(AGENTS)
        for name in targets:
            store["agents"][name] = _empty_agent_state()
        write_store(config, store)
        _clear_reservations(config, targets)
        from .calibration import reset_calibration_snapshots

        reset_calibration_snapshots(config, targets)
        # The shared cache would otherwise repopulate the cleared state on read.
        _clear_shared_probe_cache(targets)
        return store["agents"][agent] if agent is not None else store["agents"]


def _probe_codex_app_server(cwd: Path) -> dict | None:
    if not shutil.which("codex"):
        return None
    proc = None
    try:
        proc = subprocess.Popen(
            ["codex", "app-server"],
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None
        for msg in (
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"clientInfo": {"name": "agroute", "version": "0.2.0"}},
            },
            {"jsonrpc": "2.0", "method": "initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
        ):
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        deadline = time.monotonic() + PROBE_TIMEOUT_SECONDS
        pending = b""
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise AgRouteError("usage probe: Codex RPC timed out after 15 seconds")
                chunk = os.read(proc.stdout.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                if len(pending) > 1024 * 1024:
                    raise AgRouteError("usage probe: Codex RPC response exceeded 1 MiB")
                while b"\n" in pending:
                    raw_line, pending = pending.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace")
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict) and obj.get("id") == 2:
                        return parse_native_usage("codex", line)
        raise AgRouteError("usage probe: Codex RPC ended without a quota response")
    except OSError as exc:
        raise AgRouteError(f"usage probe: Codex RPC failed: {exc}") from exc
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except OSError:
                    pass
            finally:
                for handle in (proc.stdin, proc.stdout):
                    if handle is not None:
                        handle.close()
    return None


def _builtin_probe_patch(name: str, cwd: Path) -> dict | None:
    from .adapters import get_adapter

    return get_adapter(name).probe_usage(cwd)


def _record_probe_failure(config, name, exc, now):
    now_iso = format_timestamp(_now_utc(now))
    with _usage_locked(config):
        store = read_store(config)
        ag = store["agents"][name]
        probe = ag.setdefault("probe", {})
        probe.update(attempted_at=now_iso, failed_at=now_iso, error=str(exc)[:4096])
        if isinstance(exc, AgRouteAuthError):
            prev_auth = ag.get("auth") or {}
            superseded = list(prev_auth.get("superseded_events", []))
            if prev_auth.get("event_id") and prev_auth["event_id"] not in superseded:
                superseded.append(prev_auth["event_id"])
            auth_entry = {
                "status": exc.auth_status,
                "observed_at": now_iso,
                "error": exc.auth_reason,
            }
            if superseded:
                auth_entry["superseded_events"] = superseded[-256:]
            ag["auth"] = auth_entry
        write_store(config, store)
        if isinstance(exc, AgRouteAuthError):
            _write_shared_probe_cache(name, ag)


def probe_usage(config, agent=None, *, now=None):
    targets = [agent_id(agent, "agent")] if agent is not None else [
        name for name in AGENTS if config.agents[name]["enabled"]
    ]
    results = {}
    for name in targets:
        try:
            # Capture already-visible event identities before the current report.
            # Untimestamped old transcript lines cannot regain authority if their
            # file is touched after a plan upgrade.
            observed = _discover_claude_live_patch() if name == "claude" else {}
            cmd = config.agents[name]["usage_probe_cmd"]
            if not cmd:
                patch = _builtin_probe_patch(name, config.capture_root)
                if not patch:
                    raise AgRouteError(f"usage probe: agents.{name}.usage_probe_cmd is not configured and no live telemetry found")
            else:
                try:
                    proc = subprocess.run(cmd, cwd=config.capture_root, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS, check=False)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    raise AgRouteError(f"usage probe: failed to run probe command for '{name}': {exc}") from exc
                combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
                if proc.returncode != 0 and not combined:
                    raise AgRouteError(f"usage probe: command for '{name}' exited with status {proc.returncode} and no output")
                patch = parse_native_usage(name, combined, now=now)
            if patch and isinstance(patch.get("auth"), dict) and patch["auth"].get("status"):
                auth = patch["auth"]
                raise AgRouteAuthError(
                    f"usage probe: {auth['error']}",
                    auth_status=auth["status"],
                    auth_reason=auth["error"],
                )
            if not _current_probe_evidence(patch, config.agents[name], _now_utc(now)):
                raise AgRouteError(f"usage probe: no current recognized quota or availability evidence for '{name}'")
            if name == "claude":
                for key, win in _patch_windows(patch):
                    old = _get_window(observed, key)
                    if old.get("event_id") and win.get("event_id") != old["event_id"]:
                        win.setdefault("superseded_events", []).extend(old.get("superseded_events", []) + [old["event_id"]])
                if isinstance(observed.get("auth"), dict):
                    obs_auth = observed["auth"]
                    superseded_auth = list(obs_auth.get("superseded_events", []))
                    if obs_auth.get("event_id"):
                        superseded_auth.append(obs_auth["event_id"])
                    if superseded_auth:
                        patch["_superseded_auth_events"] = list(dict.fromkeys(superseded_auth))
            results[name] = apply_usage_patch(config, name, patch, source="probe", now=now)
        except (AgRouteError, OSError, subprocess.TimeoutExpired) as exc:
            if not isinstance(exc, AgRouteError):
                exc = AgRouteError(f"usage probe: provider '{name}' failed: {exc}")
            _record_probe_failure(config, name, exc, now)
            if agent is not None or config.agents[name]["usage_probe_cmd"]:
                raise exc
    return results

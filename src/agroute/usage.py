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
PROBE_TIMEOUT_SECONDS = 15
REFRESH_ATTEMPTS = 2


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
        "availability": _empty_window(),
        "scope_availability": {},
        "probe": {},
    }


def _empty_store():
    return {
        "format": USAGE_STORE_FORMAT,
        "agents": {name: _empty_agent_state() for name in AGENTS},
    }


def _now_utc(now=None):
    if now is None:
        return datetime.now(timezone.utc)
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
            ["availability", "scope_availability", "probe"],
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
        normalized["agents"][name] = {
            "session_5h": s5,
            "weekly_7d": w7,
            "rate_limited": rl,
            "cooldown_until": cd,
            "tiers": tiers,
            "updated_at": updated,
            "source": src,
            "availability": _validate_window(raw.get("availability", _empty_window()), f"{where}.availability"),
            "scope_availability": scoped,
            "probe": _validate_probe(raw.get("probe", {}), f"{where}.probe"),
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
            if not any(token in line for token in ('"rate_limit_event"', '"rate_limit_info"', '"rateLimitInfo"')):
                continue
            event_patch = {}
            if _parse_json_events(line, event_patch):
                for key, win in _patch_windows(event_patch):
                    win.setdefault("observed_at", fallback_time)
                    if win.get("used_pct") is not None:
                        win.setdefault("measured_at", win["observed_at"])
                    win["event_id"] = hashlib.sha256(f"{path}:{line_offset}:{line}".encode()).hexdigest()
                    previous = _get_window(patch, key)
                    _put_window(patch, key, _merge_window(previous, win))
                patch.setdefault("warnings", []).extend(event_patch.get("warnings", []))
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
                    for key, win in _patch_windows(cached):
                        _put_window(ag, key, _validate_window(win, f"cached.claude.{key}"))
                    for key in ("updated_at", "source", "rate_limited", "cooldown_until", "probe"):
                        if key in cached:
                            ag[key] = deepcopy(cached[key])
                    if "availability" not in cached and "probe" not in cached:
                        _migrate_legacy_claude_state(ag)
            for key, win in _patch_windows(patch):
                _put_window(ag, key, _merge_window(_get_window(ag, key), win))
            times = [win["observed_at"] for _, win in _patch_windows(ag) if win.get("observed_at")]
            if times:
                ag["updated_at"] = max(times + ([ag["updated_at"]] if ag["updated_at"] else []), key=parse_timestamp)
            if patch:
                ag["source"] = "discovered" if ag["updated_at"] is None else ag["source"]
            continue
        if (
            ag["updated_at"] is None
            and ag["session_5h"]["used_pct"] is None
            and ag["weekly_7d"]["used_pct"] is None
            and not ag["tiers"]
            and not ag["rate_limited"]
        ):
            patch = discover_live_usage_patch(name)
            if patch:
                if "session_5h" in patch and isinstance(patch["session_5h"], dict):
                    ag["session_5h"] = _validate_window(patch["session_5h"], f"discovered.{name}.session_5h")
                if "weekly_7d" in patch and isinstance(patch["weekly_7d"], dict):
                    ag["weekly_7d"] = _validate_window(patch["weekly_7d"], f"discovered.{name}.weekly_7d")
                if "rate_limited" in patch:
                    ag["rate_limited"] = bool(patch["rate_limited"])
                if "cooldown_until" in patch:
                    ag["cooldown_until"] = patch["cooldown_until"]
                if "tiers" in patch and isinstance(patch["tiers"], dict):
                    for tk, tv in patch["tiers"].items():
                        if isinstance(tv, dict):
                            ag["tiers"][tk] = _validate_window(tv, f"discovered.{name}.tiers.{tk}")
                for key in ("updated_at", "probe"):
                    if key in patch:
                        ag[key] = deepcopy(patch[key])
                times = [win["observed_at"] for _, win in _patch_windows(ag) if win.get("observed_at")]
                if times:
                    ag["updated_at"] = max(times + ([ag["updated_at"]] if ag["updated_at"] else []), key=parse_timestamp)
                ag["source"] = "discovered"
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


def usage_warnings(agent, state, cfg, now_dt):
    probe = state.get("probe", {})
    success = probe.get("succeeded_at")
    data_time = state.get("updated_at")
    references = [value for value in (success, data_time) if value]
    reference = min(references, key=parse_timestamp) if references else None
    stale = reference is None or (now_dt - parse_timestamp(reference)).total_seconds() > cfg["usage_stale_after_seconds"]
    failed = probe.get("failed_at") and (not success or parse_timestamp(probe["failed_at"]) >= parse_timestamp(success))
    warnings = list(probe.get("warnings", []))
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


def _freshness_reason(state, cfg, now_dt):
    """Check each known scope; a recent sibling never renews its clock."""
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
        observed = win.get("measured_at") if win.get("used_pct") is not None else None
        observed = observed or win.get("observed_at") or state.get("updated_at")
        if observed is None or not 0 <= (now_dt - parse_timestamp(observed)).total_seconds() <= cfg["usage_stale_after_seconds"]:
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


def routing_freshness_reason(state, cfg, now_dt):
    """Availability requires current scopes; current denial can safely block."""
    return None if _fresh_global_block(state, cfg, now_dt) else _freshness_reason(state, cfg, now_dt)


def ensure_fresh_usage(config, targets, *, now=None):
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
            if not cfg["enabled"] or _fresh_global_block(state, cfg, now_dt) or _freshness_reason(state, cfg, now_dt) is None:
                failures.pop(name, None)
            else:
                pending.append(name)
        if not any(attempts[name] < REFRESH_ATTEMPTS for name in pending):
            for name in pending:
                if name not in failures:
                    reason = _freshness_reason(store["agents"][name], config.agents[name], now_dt)
                    exc = AgRouteError(f"usage probe: refresh budget exhausted with {reason}")
                    _record_probe_failure(config, name, exc, now)
                    failures[name] = (f"{name} quota refresh infrastructure incident after {REFRESH_ATTEMPTS} attempts: "
                                      f"{exc}; retry with agroute usage probe --agent {name}")
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
                reason = None if _fresh_global_block(state, cfg, now_dt) else _freshness_reason(state, cfg, now_dt)
                if reason is not None:
                    raise AgRouteError(f"usage probe: {reason} after partial report")
            except AgRouteError as exc:
                _record_probe_failure(config, name, exc, now)
                failures[name] = (f"{name} quota refresh infrastructure incident after {REFRESH_ATTEMPTS} attempts: "
                                  f"{exc}; retry with agroute usage probe --agent {name}")
            else:
                failures.pop(name, None)
        # Another provider's probe may cross this provider's reset or age
        # boundary. Recheck everyone without renewing any attempt budget.
        store = read_store(config)
    return read_store(config), failures


def effective_agent_state(agent_state, now_dt, *, project_rejections=True):
    state = deepcopy(agent_state)
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
    for key in keys_to_check:
        tier_win = effective_state["tiers"].get(key)
        if tier_win is not None and tier_win["used_pct"] is not None:
            if tier_win["used_pct"] >= max_tier:
                return (
                    False,
                    f"tier '{key}' used_pct ({tier_win['used_pct']:.1f}%) >= max_tier_used_pct ({max_tier:.1f}%)",
                )
    return True, None


def resolve_model_quota(agent_name, agent_cfg, effective_state, candidate, *, is_host=False, surface="cli"):
    """Resolve effective 5h/7d quota windows and eligibility for a specific model."""
    model_id = str(candidate["model"])
    from .adapters import get_adapter

    try:
        adapter = get_adapter(agent_name)
        quota_group, s5, w7 = adapter.resolve_quota_group(model_id, candidate, effective_state)
    except Exception:
        s5 = deepcopy(effective_state["session_5h"])
        w7 = deepcopy(effective_state["weekly_7d"])
        quota_group = "shared"

    rejection = (_rejection_reason(agent_name, effective_state)
                 or _scoped_access_rejection(agent_name, effective_state, candidate)
                 or _window_rejection_reason(agent_name, quota_group, s5, w7))
    if rejection and agent_cfg["enabled"]:
        return {"model": model_id, "surface": surface, "quota_group": quota_group,
                "session_5h": s5, "weekly_7d": w7, "eligible": False,
                "exclusion_reason": rejection}
    if not agent_cfg["enabled"]:
        return {
            "model": model_id,
            "surface": surface,
            "quota_group": quota_group,
            "session_5h": s5,
            "weekly_7d": w7,
            "eligible": False,
            "exclusion_reason": "agent is disabled in configuration",
        }
    if effective_state["rate_limited"]:
        until = effective_state["cooldown_until"]
        suffix = f" until {until}" if until else ""
        return {
            "model": model_id,
            "surface": surface,
            "quota_group": quota_group,
            "session_5h": s5,
            "weekly_7d": w7,
            "eligible": False,
            "exclusion_reason": f"agent is rate-limited{suffix}",
        }
    if effective_state["cooldown_until"] is not None:
        return {
            "model": model_id,
            "surface": surface,
            "quota_group": quota_group,
            "session_5h": s5,
            "weekly_7d": w7,
            "eligible": False,
            "exclusion_reason": f"agent is in cooldown until {effective_state['cooldown_until']}",
        }

    reserve = agent_cfg["host_reserve_pct"] if is_host else 0.0
    max_session = max(0.0, agent_cfg["max_session_used_pct"] - reserve)
    max_weekly = max(0.0, agent_cfg["max_weekly_used_pct"] - reserve)

    ok_tier, reason_tier = evaluate_candidate_tier(agent_cfg, effective_state, candidate)
    if not ok_tier:
        return {
            "model": model_id,
            "surface": surface,
            "quota_group": quota_group,
            "session_5h": s5,
            "weekly_7d": w7,
            "eligible": False,
            "exclusion_reason": reason_tier,
        }

    if s5["used_pct"] is not None and s5["used_pct"] >= max_session:
        return {
            "model": model_id,
            "surface": surface,
            "quota_group": quota_group,
            "session_5h": s5,
            "weekly_7d": w7,
            "eligible": False,
            "exclusion_reason": f"session_5h used_pct ({s5['used_pct']:.1f}%) >= effective limit ({max_session:.1f}%)",
        }
    if w7["used_pct"] is not None and w7["used_pct"] >= max_weekly:
        return {
            "model": model_id,
            "surface": surface,
            "quota_group": quota_group,
            "session_5h": s5,
            "weekly_7d": w7,
            "eligible": False,
            "exclusion_reason": f"weekly_7d used_pct ({w7['used_pct']:.1f}%) >= effective limit ({max_weekly:.1f}%)",
        }

    return {
        "model": model_id,
        "surface": surface,
        "quota_group": quota_group,
        "session_5h": s5,
        "weekly_7d": w7,
        "eligible": True,
        "exclusion_reason": None,
    }


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


def _apply_agy_usage_command(cmd_data: dict, patch: dict) -> bool:
    groups = cmd_data.get("groups")
    if not isinstance(groups, list):
        return False
    changed = False
    tiers = patch.setdefault("tiers", {})
    for grp in groups:
        if not isinstance(grp, dict):
            continue
        grp_name = str(grp.get("name") or "").strip().lower()
        buckets = grp.get("buckets")
        if not isinstance(buckets, list):
            continue
        for bkt in buckets:
            if not isinstance(bkt, dict):
                continue
            bid = str(bkt.get("id") or "").strip().lower()
            window = str(bkt.get("window") or "").strip().lower()
            win = _extract_window_dict(bkt, f"agy.command.data.{bid or 'bucket'}")
            if win["used_pct"] is None and win["reset_at"] is None:
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


def _parse_json_events(raw_text, patch):
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
    for obj in parsed_objects:
        event_patch = {}
        recognized = _parse_json_object(obj, event_patch)
        changed = recognized or changed
        observed = obj.get("timestamp", obj.get("updated_at"))
        if recognized and observed is not None:
            observed = format_timestamp(parse_timestamp(observed, "usage event timestamp"))
            for _, win in _patch_windows(event_patch):
                win.setdefault("observed_at", observed)
                if win.get("used_pct") is not None:
                    win.setdefault("measured_at", win["observed_at"])
        for key, win in _patch_windows(event_patch):
            _put_window(patch, key, _merge_window(_get_window(patch, key), win))
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
    return changed


def _parse_json_object(obj, patch):
    """Normalize one external event before combining independent observations."""
    changed = False
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
        if _apply_agy_usage_command(cmd_obj["data"], patch):
            changed = True
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
    if obj.get("type") == "result" and obj.get("is_error") is True:
        err_text = str(obj.get("result") or "").lower()
        if (
            obj.get("api_error_status") in (401, 403, 429)
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
        reset_match = _RESET_RE.search(line)
        reset_iso = (
            format_timestamp(parse_timestamp(reset_match.group(1), "text reset_at"))
            if reset_match
            else None
        )
        if any(phrase in lower for phrase in ("rate limit reached", "quota exceeded", "usage limit reached")):
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


def parse_native_usage(agent, raw_text):
    agent_id(agent, "agent")
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise AgRouteError("usage ingest: input text is empty")
    patch = {}
    recognized = _parse_json_events(raw_text, patch)
    if not recognized:
        recognized = _parse_text_usage(raw_text, patch)
    if not recognized:
        raise AgRouteError(f"usage ingest: no recognizable native usage or rate-limit data found for '{agent}'")
    if agent == "claude" and patch.get("rate_limited") is True:
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
        # Snapshot time describes quota evidence; failures never refresh it.
        if source in ("ingest", "probe", "set"):
            times = [win["observed_at"] for _, win in _patch_windows(current) if win.get("observed_at")]
            if current["updated_at"]:
                times.append(current["updated_at"])
            if "rate_limited" in patch or "cooldown_until" in patch:
                times.append(patch.get("updated_at") or format_timestamp(now_dt))
            current["updated_at"] = max(times, key=parse_timestamp) if times else format_timestamp(now_dt)
        else:
            current["updated_at"] = format_timestamp(now_dt)
        current["source"] = source
        if source == "probe":
            current["probe"].update(attempted_at=format_timestamp(now_dt), succeeded_at=format_timestamp(now_dt),
                                    warnings=patch.get("warnings", []))
            current["probe"].pop("error", None)
            current["probe"].pop("failed_at", None)
        store["agents"][agent] = current
        write_store(config, store)
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
    with _usage_locked(config):
        store = read_store(config)
        probe = store["agents"][name].setdefault("probe", {})
        probe.update(attempted_at=format_timestamp(_now_utc(now)), failed_at=format_timestamp(_now_utc(now)),
                     error=str(exc)[:4096])
        write_store(config, store)


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
                patch = parse_native_usage(name, combined)
            if not _current_probe_evidence(patch, config.agents[name], _now_utc(now)):
                raise AgRouteError(f"usage probe: no current recognized quota or availability evidence for '{name}'")
            if name == "claude":
                for key, win in _patch_windows(patch):
                    old = _get_window(observed, key)
                    if old.get("event_id") and win.get("event_id") != old["event_id"]:
                        win.setdefault("superseded_events", []).extend(old.get("superseded_events", []) + [old["event_id"]])
            results[name] = apply_usage_patch(config, name, patch, source="probe", now=now)
        except (AgRouteError, OSError, subprocess.TimeoutExpired) as exc:
            if not isinstance(exc, AgRouteError):
                exc = AgRouteError(f"usage probe: provider '{name}' failed: {exc}")
            _record_probe_failure(config, name, exc, now)
            if agent is not None or config.agents[name]["usage_probe_cmd"]:
                raise exc
    return results

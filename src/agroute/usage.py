"""Lightweight native quota and rate-limit tracking across Codex, Claude Code, and Antigravity."""

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

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
     percentage,
    parse_timestamp,
    table,
    text,
)

USAGE_STORE_FORMAT = "agroute-usage-store-v1"


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
    keys(win, ["used_pct", "reset_at"], where=where)
    used = win["used_pct"]
    if used is not None:
        used = percentage(used, f"{where}.used_pct")
    reset_at = win["reset_at"]
    if reset_at is not None:
        reset_at = format_timestamp(parse_timestamp(reset_at, f"{where}.reset_at"))
    return {"used_pct": used, "reset_at": reset_at}


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
        }
    return normalized


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
                return patch
    return {}


def _discover_claude_live_patch() -> dict:
    """Discover latest rate_limit_event from ${CLAUDE_CONFIG_DIR:-~/.claude}/projects JSONL files."""
    projects_dir = _claude_home() / "projects"
    if not projects_dir.is_dir():
        return {}
    try:
        files = sorted(
            projects_dir.rglob("*.jsonl"),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )[:5]
    except OSError:
        return {}
    for path in files:
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
            if '"rate_limit_event"' not in line and '"rate_limit_info"' not in line:
                continue
            patch: dict = {}
            if _parse_json_events(line, patch) and patch:
                return patch
    return {}


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
                ag["source"] = "discovered"
    return store


def write_store(config, store):
    validated = _validate_store_payload(store)
    claim(config)
    path = config.state_root / "usage.json"
    atomic_write(path, json.dumps(validated, indent=2, sort_keys=True) + "\n")
    return validated


def effective_agent_state(agent_state, now_dt):
    state = deepcopy(agent_state)
    for win_key in ("session_5h", "weekly_7d"):
        win = state[win_key]
        if win["reset_at"] is not None and now_dt >= parse_timestamp(win["reset_at"]):
            state[win_key] = _empty_window()
    if state["cooldown_until"] is not None and now_dt >= parse_timestamp(state["cooldown_until"]):
        state["cooldown_until"] = None
        state["rate_limited"] = False
    active_tiers = {}
    for tier_name, tier_win in state["tiers"].items():
        if tier_win["reset_at"] is not None and now_dt >= parse_timestamp(tier_win["reset_at"]):
            continue
        active_tiers[tier_name] = tier_win
    state["tiers"] = active_tiers
    return state


def evaluate_agent_quota(agent_name, agent_cfg, effective_state, *, is_host=False):
    """Return (eligible: bool, reason: str | None) for the agent as a whole."""
    if not agent_cfg["enabled"]:
        return False, "agent is disabled in configuration"
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
    return {"used_pct": used, "reset_at": reset_at}


def _apply_rate_limits_dict(rl_obj, patch, where="rate_limits"):
    table(rl_obj, where)
    changed = False
    for slot in ("primary", "secondary", "session_5h", "weekly_7d"):
        if slot in rl_obj and isinstance(rl_obj[slot], dict):
            sub = rl_obj[slot]
            win = _extract_window_dict(sub, f"{where}.{slot}")
            win_mins = sub.get("window_minutes", sub.get("windowDurationMins"))
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


def _apply_claude_usage_report(report: dict, patch: dict) -> bool:
    rl = report.get("rate_limits")
    if not isinstance(rl, dict):
        return False
    limits = rl.get("limits")
    if not isinstance(limits, list):
        return False
    changed = False
    tiers = patch.setdefault("tiers", {})
    for item in limits:
        if not isinstance(item, dict):
            continue
        win = _extract_window_dict(item, "claude.usage_report.limits")
        if win["used_pct"] is None and win["reset_at"] is None:
            continue
        scope = item.get("scope")
        kind = str(item.get("kind") or "").strip().lower()
        group = str(item.get("group") or "").strip().lower()
        if scope:
            scope_key = str(scope).strip().lower()
            tiers[scope_key] = win
            if group == "session" or "5h" in kind or "session" in kind:
                tiers[f"{scope_key}-5h"] = win
            else:
                tiers[f"{scope_key}-weekly"] = win
            changed = True
        elif kind in ("session", "five_hour", "5h") or group == "session":
            patch["session_5h"] = win
            changed = True
        elif kind in ("weekly_all", "weekly", "seven_day", "7d") or group == "weekly":
            patch["weekly_7d"] = win
            changed = True
        elif kind.startswith(("weekly_", "seven_day_")):
            suffix = kind.split("_", 1)[1].replace("day_", "").strip()
            if suffix and suffix != "all":
                tiers[suffix] = win
                tiers[f"{suffix}-weekly"] = win
                changed = True
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
            if _apply_claude_usage_report(usage_rep, patch):
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
        if obj.get("type") == "rate_limit_event" or "rate_limit_info" in obj:
            rli = obj.get("rate_limit_info", obj)
            if isinstance(rli, dict):
                status = rli.get("status")
                resets_raw = rli.get("resets_at", rli.get("reset_at", rli.get("resetsAt")))
                reset_iso = (
                    format_timestamp(parse_timestamp(resets_raw, "rate_limit_info.resets_at"))
                    if resets_raw is not None
                    else None
                )
                if status in ("rejected", "rate_limited", "exhausted") or rli.get("rate_limited") is True:
                    patch["rate_limited"] = True
                    if reset_iso is not None:
                        patch["cooldown_until"] = reset_iso
                    changed = True
                if _apply_rate_limits_dict(rli, patch, "rate_limit_info"):
                    changed = True
                unified = rli.get("unifiedWindows")
                if isinstance(unified, dict):
                    if isinstance(unified.get("five_hour"), dict):
                        win5 = _extract_window_dict(unified["five_hour"], "rate_limit_info.unifiedWindows.five_hour")
                        if win5["used_pct"] is not None or win5["reset_at"] is not None:
                            patch["session_5h"] = win5
                            changed = True
                    if isinstance(unified.get("seven_day"), dict):
                        win7 = _extract_window_dict(unified["seven_day"], "rate_limit_info.unifiedWindows.seven_day")
                        if win7["used_pct"] is not None or win7["reset_at"] is not None:
                            patch["weekly_7d"] = win7
                            changed = True
                win_type = rli.get("rate_limit_type", rli.get("rateLimitType", rli.get("window")))
                if win_type in ("five_hour", "session_5h", "5h") and "session_5h" not in patch:
                    win = _extract_window_dict(rli, "rate_limit_info")
                    if win["used_pct"] is not None or win["reset_at"] is not None:
                        patch["session_5h"] = win
                        changed = True
                elif win_type in ("seven_day", "weekly_7d", "7d", "weekly") and "weekly_7d" not in patch:
                    win = _extract_window_dict(rli, "rate_limit_info")
                    if win["used_pct"] is not None or win["reset_at"] is not None:
                        patch["weekly_7d"] = win
                        changed = True
        if obj.get("type") == "result" and obj.get("is_error") is True:
            err_text = str(obj.get("result") or "").lower()
            if (
                obj.get("api_error_status") in (401, 403, 429)
                or obj.get("terminal_reason") in ("api_error", "rate_limit")
                or any(k in err_text for k in ("rate limit", "quota", "subscription", "usage limit"))
            ):
                patch["rate_limited"] = True
                changed = True
        if obj.get("event") == "result" and isinstance(obj.get("result"), dict):
            res_obj = obj["result"]
            if res_obj.get("status") == "SUCCESS":
                patch.setdefault("rate_limited", False)
                changed = True
            elif res_obj.get("status") in ("ERROR", "FAILED", "RATE_LIMITED"):
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
    if not _parse_json_events(raw_text, patch):
        _parse_text_usage(raw_text, patch)
    if not patch:
        raise AgRouteError(f"usage ingest: no recognizable native usage or rate-limit data found for '{agent}'")
    return patch


def apply_usage_patch(config, agent, patch, *, source="ingest", now=None):
    agent_id(agent, "agent")
    now_dt = _now_utc(now)
    with _usage_locked(config):
        store = _read_raw_store(config)
        current = effective_agent_state(store["agents"][agent], now_dt)
        if "session_5h" in patch:
            s5 = patch["session_5h"]
            if s5.get("used_pct") is not None:
                current["session_5h"]["used_pct"] = percentage(s5["used_pct"], "session_5h.used_pct")
            if s5.get("reset_at") is not None:
                current["session_5h"]["reset_at"] = format_timestamp(
                    parse_timestamp(s5["reset_at"], "session_5h.reset_at")
                )
        if "weekly_7d" in patch:
            w7 = patch["weekly_7d"]
            if w7.get("used_pct") is not None:
                current["weekly_7d"]["used_pct"] = percentage(w7["used_pct"], "weekly_7d.used_pct")
            if w7.get("reset_at") is not None:
                current["weekly_7d"]["reset_at"] = format_timestamp(
                    parse_timestamp(w7["reset_at"], "weekly_7d.reset_at")
                )
        if "rate_limited" in patch and patch["rate_limited"] is not None:
            current["rate_limited"] = boolean(patch["rate_limited"], "rate_limited")
            if not current["rate_limited"] and "cooldown_until" not in patch:
                current["cooldown_until"] = None
        if "cooldown_until" in patch:
            cd = patch["cooldown_until"]
            current["cooldown_until"] = (
                format_timestamp(parse_timestamp(cd, "cooldown_until")) if cd is not None else None
            )
            if current["cooldown_until"] is not None and "rate_limited" not in patch:
                current["rate_limited"] = True
        if "tiers" in patch and isinstance(patch["tiers"], dict):
            for tier_name, tier_win in patch["tiers"].items():
                text(tier_name, f"tiers.{tier_name}", label=True, max_length=64)
                existing_tier = current["tiers"].get(tier_name, _empty_window())
                if tier_win.get("used_pct") is not None:
                    existing_tier["used_pct"] = percentage(
                        tier_win["used_pct"], f"tiers.{tier_name}.used_pct"
                    )
                if tier_win.get("reset_at") is not None:
                    existing_tier["reset_at"] = format_timestamp(
                        parse_timestamp(tier_win["reset_at"], f"tiers.{tier_name}.reset_at")
                    )
                current["tiers"][tier_name] = existing_tier
        current = effective_agent_state(current, now_dt)
        current["updated_at"] = format_timestamp(now_dt)
        current["source"] = source
        store["agents"][agent] = current
        write_store(config, store)
        if source in ("probe", "ingest"):
            _write_shared_probe_cache(agent, current)
        return current


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
            stderr=subprocess.PIPE,
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
        for _ in range(16):
            line = proc.stdout.readline()
            if not line:
                break
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("id") == 2:
                return parse_native_usage("codex", line)
    except (OSError, AgRouteError):
        return None
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    proc.kill()
                except OSError:
                    pass
    return None


def _builtin_probe_patch(name: str, cwd: Path) -> dict | None:
    from .adapters import get_adapter

    try:
        return get_adapter(name).probe_usage(cwd)
    except Exception:
        return None


def probe_usage(config, agent=None, *, now=None):
    targets = [agent_id(agent, "agent")] if agent is not None else [
        name for name in AGENTS if config.agents[name]["enabled"]
    ]
    results = {}
    for name in targets:
        cmd = config.agents[name]["usage_probe_cmd"]
        if not cmd:
            patch = _builtin_probe_patch(name, config.capture_root)
            if patch is not None:
                results[name] = apply_usage_patch(config, name, patch, source="probe", now=now)
            elif agent is not None:
                raise AgRouteError(f"usage probe: agents.{name}.usage_probe_cmd is not configured and no live telemetry found")
            continue
        try:
            proc = subprocess.run(
                cmd,
                cwd=config.capture_root,
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AgRouteError(f"usage probe: failed to run probe command for '{name}': {exc}") from exc
        combined = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        if proc.returncode != 0 and not combined:
            raise AgRouteError(
                f"usage probe: command for '{name}' exited with status {proc.returncode} and no output"
            )
        patch = parse_native_usage(name, combined)
        results[name] = apply_usage_patch(config, name, patch, source="probe", now=now)
    return results

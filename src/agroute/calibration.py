"""Empirical Bayes calibration of 5-hour and weekly subscription credit-pool ceilings.

`agroute` tracks quota snapshots per `(agent, effective_plan, quota_group, window)` across
non-resetting windows (`<600s` `reset_at` jitter tolerance; rollover when `reset_at` shifts
by `>=600s`, `reset_at` passes, or `used_pct` drops). When a non-resetting interval
`[t_prev, t_curr]` records `delta_used_pct >= 0.25` (`delta_frac = delta_used_pct / 100.0`),
no unobserved or in-flight planned route straddles `t_curr`, and all completed `howhard`
observations for `(agent, quota_group)` in `(t_prev, t_curr]` carry positive `cost_usd`,
the interval updates bounded sufficient statistics `(S_cost, S_frac)` (`S_frac <= 2.0`)
with outlier filtering `[0.1 * C_prior, 10.0 * C_prior]` and empirical Bayes shrinkage
`C_hat = (F0 * C_prior + S_cost) / (F0 + S_frac)` (`F0 = 0.20`). Explicit manual
`[agents.<name>].credit` overrides remain exact.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import json
import math
import os
from pathlib import Path

from .adapters import get_adapter
from .config import resolve_plan_credit_sources
from .files import atomic_write
from .state import check, claim
from .validation import (
    AGENTS,
    AgRouteError,
    agent_id,
    format_timestamp,
    keys,
    number,
    parse_timestamp,
    percentage,
    table,
    text,
)

CALIBRATION_STORE_FORMAT = "agroute-credit-calibration-v1"
WINDOWS = ("session_5h", "weekly_7d")
PRIOR_STRENGTH_F0 = 0.20
MAX_SAMPLE_WINDOWS_FMAX = 2.0
MIN_DELTA_USED_PCT = 0.25
RESET_JITTER_TOLERANCE_SECONDS = 600.0
OUTLIER_MIN_FACTOR = 0.1
OUTLIER_MAX_FACTOR = 10.0
UNOBSERVED_ROUTE_TTL_SECONDS = 18000.0
_MAX_OUTCOMES_TAIL_BYTES = 4 * 1024 * 1024
_OBSERVED_OUTCOMES = frozenset({"clean", "loud", "silent"})

_OBS_CACHE: dict[str, tuple[tuple[int, int] | None, tuple[int, int] | None, tuple[dict[str, dict], list[dict]]]] = {}
_ROUTES_CACHE: dict[str, tuple[tuple[int, int] | None, list[dict]]] = {}


def calibration_path(config) -> Path:
    return config.state_root / "credit_calibration.json"


def make_pool_key(agent: str, plan: str, quota_group: str, window: str) -> str:
    return f"{agent}:{plan}:{quota_group}:{window}"


def _empty_calibration_store() -> dict:
    return {"format": CALIBRATION_STORE_FORMAT, "pools": {}}


def _validate_snapshot_entry(raw: dict, where: str) -> dict:
    table(raw, where)
    keys(raw, ["observed_at", "used_pct", "reset_at"], where=where)
    observed_at = format_timestamp(parse_timestamp(raw["observed_at"], f"{where}.observed_at"))
    used_pct = percentage(raw["used_pct"], f"{where}.used_pct")
    reset_at = raw["reset_at"]
    if reset_at is not None:
        reset_at = format_timestamp(parse_timestamp(reset_at, f"{where}.reset_at"))
    return {
        "observed_at": observed_at,
        "used_pct": used_pct,
        "reset_at": reset_at,
    }


def _validate_calibration_store(payload: dict, path: Path) -> dict:
    where = f"credit calibration store {path}"
    table(payload, where)
    keys(payload, ["format", "pools"], where=where)
    if payload["format"] != CALIBRATION_STORE_FORMAT:
        raise AgRouteError(f"{where}: unsupported format {payload['format']!r}")
    pools_raw = table(payload["pools"], f"{where}.pools")
    pools: dict[str, dict] = {}
    for raw_key, entry in pools_raw.items():
        p_where = f"{where}.pools.{raw_key}"
        table(entry, p_where)
        keys(
            entry,
            ["agent", "plan", "quota_group", "window", "s_cost", "s_frac", "matched_intervals"],
            ["rejected_outliers", "updated_at", "last_snapshot"],
            where=p_where,
        )
        ag = agent_id(entry["agent"], f"{p_where}.agent")
        plan = text(entry["plan"], f"{p_where}.plan", label=True, max_length=64)
        q_group = text(entry["quota_group"], f"{p_where}.quota_group", label=True, max_length=64).lower()
        win = text(entry["window"], f"{p_where}.window", label=True, max_length=32)
        if win not in WINDOWS:
            raise AgRouteError(f"{p_where}.window: expected one of {list(WINDOWS)}, got {win!r}")
        expected_key = make_pool_key(ag, plan, q_group, win)
        if raw_key != expected_key:
            raise AgRouteError(f"{p_where}: pool key {raw_key!r} does not match {expected_key!r}")
        s_cost = number(entry["s_cost"], f"{p_where}.s_cost", nonnegative=True)
        s_frac = number(entry["s_frac"], f"{p_where}.s_frac", nonnegative=True)
        if s_frac > MAX_SAMPLE_WINDOWS_FMAX + 1e-6:
            raise AgRouteError(f"{p_where}.s_frac: exceeds maximum {MAX_SAMPLE_WINDOWS_FMAX}")
        matched = entry["matched_intervals"]
        if isinstance(matched, bool) or not isinstance(matched, int) or matched < 0:
            raise AgRouteError(f"{p_where}.matched_intervals: expected a non-negative integer")
        rejected = entry.get("rejected_outliers", 0)
        if isinstance(rejected, bool) or not isinstance(rejected, int) or rejected < 0:
            raise AgRouteError(f"{p_where}.rejected_outliers: expected a non-negative integer")
        updated_at = entry.get("updated_at")
        if updated_at is not None:
            updated_at = format_timestamp(parse_timestamp(updated_at, f"{p_where}.updated_at"))
        last_snap = entry.get("last_snapshot")
        if last_snap is not None:
            last_snap = _validate_snapshot_entry(last_snap, f"{p_where}.last_snapshot")
        pools[expected_key] = {
            "agent": ag,
            "plan": plan,
            "quota_group": q_group,
            "window": win,
            "s_cost": min(s_cost, 1e12),
            "s_frac": min(s_frac, MAX_SAMPLE_WINDOWS_FMAX),
            "matched_intervals": matched,
            "rejected_outliers": rejected,
            "updated_at": updated_at,
            "last_snapshot": last_snap,
        }
    return {"format": CALIBRATION_STORE_FORMAT, "pools": pools}


def read_calibration_store(config) -> dict:
    """Read and validate `<state_root>/credit_calibration.json`, or return an empty store."""
    check(config)
    path = calibration_path(config)
    if not path.exists():
        return _empty_calibration_store()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError) as exc:
        raise AgRouteError(f"credit calibration store: invalid JSON at {path}: {exc}") from exc
    return _validate_calibration_store(raw, path)


def write_calibration_store(config, store: dict) -> dict:
    """Validate and atomically persist `<state_root>/credit_calibration.json` (caller holds `_usage_locked`)."""
    path = calibration_path(config)
    normalized = _validate_calibration_store(store, path)
    claim(config)
    atomic_write(path, json.dumps(normalized, indent=2, sort_keys=True) + "\n")
    return normalized


def _stat_sig(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _read_jsonl_tail(path: Path, *, max_bytes: int | None = None) -> list[dict]:
    if not path.is_file():
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            if max_bytes is not None and size > max_bytes:
                handle.seek(size - max_bytes)
                raw = handle.read()
                nl = raw.find(b"\n")
                raw = raw[nl + 1 :] if nl >= 0 else b""
            else:
                handle.seek(0)
                raw = handle.read()
        text_body = raw.decode("utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text_body.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _extract_observation_cost_usd(obs: dict) -> float | None:
    raw_cost = obs.get("cost_usd")
    if type(raw_cost) in (int, float) and math.isfinite(raw_cost):
        return float(raw_cost)
    eu = obs.get("execution_usage")
    if isinstance(eu, dict):
        prov = eu.get("provenance")
        segs = eu.get("segments")
        if isinstance(prov, dict) and prov.get("completed") is True and isinstance(segs, list) and segs:
            for field in ("billed_cost", "estimated_cost"):
                vals = []
                ok = True
                for seg in segs:
                    c_obj = seg.get(field) if isinstance(seg, dict) else None
                    usd = c_obj.get("usd") if isinstance(c_obj, dict) else None
                    if type(usd) in (int, float) and math.isfinite(usd) and usd >= 0:
                        vals.append(float(usd))
                    else:
                        ok = False
                        break
                if ok and vals:
                    return sum(vals)
    return None


def _load_hh_observations(hh_root: Path) -> tuple[dict[str, dict], list[dict]]:
    outcomes_path = hh_root / "outcomes.jsonl"
    pending_path = hh_root / "pending.jsonl"
    sig_out = _stat_sig(outcomes_path)
    sig_pend = _stat_sig(pending_path)
    cache_key = str(hh_root)
    cached = _OBS_CACHE.get(cache_key)
    if cached is not None and cached[0] == sig_out and cached[1] == sig_pend:
        return cached[2]

    raw_rows = _read_jsonl_tail(outcomes_path, max_bytes=_MAX_OUTCOMES_TAIL_BYTES)
    raw_rows.extend(_read_jsonl_tail(pending_path))
    if not raw_rows:
        result: tuple[dict[str, dict], list[dict]] = ({}, [])
        _OBS_CACHE[cache_key] = (sig_out, sig_pend, result)
        return result

    by_id: dict[str, dict] = {}
    ordered_ids: list[str] = []
    for item in raw_rows:
        obs_id = item.get("id")
        if not isinstance(obs_id, str) or not obs_id:
            continue
        amends = item.get("amends")
        if isinstance(amends, str) and amends:
            if amends not in by_id:
                continue
            prior = by_id[amends]
            merged = deepcopy(prior)
            for field in ("outcome", "cause", "note"):
                if field in item:
                    merged[field] = item[field]
            corr = item.get("measurement_corrections")
            if isinstance(corr, dict):
                for m_field in ("tokens", "seconds", "cost_usd", "execution_usage"):
                    if m_field in corr:
                        merged[m_field] = deepcopy(corr[m_field])
            else:
                for m_field in ("tokens", "seconds", "cost_usd", "execution_usage"):
                    if item.get(m_field) is not None:
                        merged[m_field] = deepcopy(item[m_field])
            by_id[amends] = merged
            by_id[obs_id] = merged
        else:
            entry = deepcopy(item)
            entry["_orig_observed_at"] = item.get("observed_at")
            by_id[obs_id] = entry
            ordered_ids.append(obs_id)

    from .routes import infer_agent_from_model

    obs_by_decision: dict[str, dict] = {}
    effective_list: list[dict] = []
    seen_root: set[str] = set()
    for root_id in ordered_ids:
        if root_id in seen_root:
            continue
        seen_root.add(root_id)
        final_obs = by_id[root_id]
        if final_obs.get("outcome") not in _OBSERVED_OUTCOMES:
            continue
        obs_at_raw = final_obs.get("_orig_observed_at") or final_obs.get("observed_at")
        if not isinstance(obs_at_raw, str):
            continue
        try:
            obs_dt = parse_timestamp(obs_at_raw, "howhard observed_at")
        except AgRouteError:
            continue
        raw_model = str(final_obs.get("model") or "").strip()
        if not raw_model:
            continue
        exec_agent, bare_model = infer_agent_from_model(raw_model)
        cost_usd = _extract_observation_cost_usd(final_obs)
        normalized_obs = {
            "id": root_id,
            "decision_id": final_obs.get("decision_id") if isinstance(final_obs.get("decision_id"), str) else None,
            "agent": exec_agent,
            "model": bare_model,
            "effort": str(final_obs.get("effort") or "standard"),
            "outcome": str(final_obs["outcome"]),
            "observed_dt": obs_dt,
            "observed_at": format_timestamp(obs_dt),
            "cost_usd": cost_usd,
        }
        effective_list.append(normalized_obs)
        if normalized_obs["decision_id"]:
            obs_by_decision[normalized_obs["decision_id"]] = normalized_obs

    result = (obs_by_decision, effective_list)
    _OBS_CACHE[cache_key] = (sig_out, sig_pend, result)
    return result


def _load_routes_file(routes_path: Path) -> list[dict]:
    sig = _stat_sig(routes_path)
    key = str(routes_path)
    cached = _ROUTES_CACHE.get(key)
    if cached is not None and cached[0] == sig:
        return cached[1]
    raw_rows = _read_jsonl_tail(routes_path)
    parsed: list[dict] = []
    for rec in raw_rows:
        route_id = rec.get("route_id")
        created_raw = rec.get("created_at")
        if not isinstance(route_id, str) or not route_id or not isinstance(created_raw, str):
            continue
        try:
            created_dt = parse_timestamp(created_raw, "route.created_at")
        except AgRouteError:
            continue
        resolved = rec.get("resolved_target") if isinstance(rec.get("resolved_target"), dict) else {}
        target_agent = str(rec.get("target_agent") or resolved.get("agent") or "")
        model = str(resolved.get("model") or "")
        if target_agent not in AGENTS or not model:
            continue
        parsed.append(
            {
                "route_id": route_id,
                "decision_id": rec.get("decision_id") if isinstance(rec.get("decision_id"), str) else None,
                "agent": target_agent,
                "model": model,
                "created_dt": created_dt,
            }
        )
    _ROUTES_CACHE[key] = (sig, parsed)
    return parsed


def _collect_telemetry_sources(config) -> tuple[dict[str, dict], list[dict], list[dict]]:
    """Collect deduplicated howhard observations and agroute plan records for `config`."""
    repos = [
        {
            "capture_root": str(config.capture_root),
            "state_root": str(config.state_root),
        }
    ]
    if not getattr(config, "explicit_state", False):
        from .routes import _read_cached_json, _spool_dir

        spool = _spool_dir()
        if spool is not None:
            reg = _read_cached_json(spool / "repositories.json")
            if reg and isinstance(reg.get("repositories"), list):
                seen_states = {str(config.state_root)}
                for item in reg["repositories"]:
                    if not isinstance(item, dict):
                        continue
                    sr, cr = item.get("state_root"), item.get("capture_root")
                    if isinstance(sr, str) and isinstance(cr, str) and sr not in seen_states and Path(cr).exists():
                        seen_states.add(sr)
                        repos.append({"capture_root": cr, "state_root": sr})

    hh_roots: list[Path] = []
    seen_hh: set[str] = set()
    env_hh = os.environ.get("HOWHARD_STATE_DIR")
    if env_hh:
        p = Path(env_hh).expanduser().resolve()
        if p.is_dir():
            seen_hh.add(str(p))
            hh_roots.append(p)

    route_files: list[Path] = []
    seen_rf: set[str] = set()
    for repo in repos:
        sr = Path(repo["state_root"])
        cr = Path(repo["capture_root"])
        rf = sr / "routes.jsonl"
        if str(rf) not in seen_rf and rf.is_file():
            seen_rf.add(str(rf))
            route_files.append(rf)
        candidates = [sr.parent / "howhard", cr / ".howhard"]
        if not getattr(config, "explicit_state", False):
            candidates.insert(1, cr / ".git" / "howhard")
        for cand in candidates:
            resolved = cand.resolve()
            if str(resolved) not in seen_hh and resolved.is_dir():
                seen_hh.add(str(resolved))
                hh_roots.append(resolved)

    obs_by_decision: dict[str, dict] = {}
    obs_by_id: dict[str, dict] = {}
    for hh_root in hh_roots:
        dec_map, obs_list = _load_hh_observations(hh_root)
        obs_by_decision.update(dec_map)
        for obs in obs_list:
            obs_by_id[obs["id"]] = obs

    routes_by_id: dict[str, dict] = {}
    for rf in route_files:
        for rec in _load_routes_file(rf):
            routes_by_id[rec["route_id"]] = rec

    return obs_by_decision, list(obs_by_id.values()), list(routes_by_id.values())


def _canonical_pool_group_for_window(
    agent_name: str,
    quota_group: str,
    window: str,
    preset_credit: dict,
    manual_credit: dict,
) -> tuple[str, float | None, float | None]:
    """Return `(pool_group, prior_usd, manual_usd)` for `(agent_name, quota_group, window)`."""
    field = f"{window}_usd"
    p_tiers = preset_credit.get("tiers") or {}
    m_tiers = manual_credit.get("tiers") or {}

    candidate_groups = [quota_group]
    if "-" in quota_group:
        candidate_groups.append(quota_group.split("-", 1)[0])
    if agent_name == "agy" and quota_group in ("argon", "carbon", "hydrogen"):
        candidate_groups.append("gemini")

    manual_usd = None
    manual_group = None
    for grp in candidate_groups:
        if grp in m_tiers and field in m_tiers[grp]:
            manual_usd = float(m_tiers[grp][field])
            manual_group = grp
            break
    if manual_usd is None and field in manual_credit:
        manual_usd = float(manual_credit[field])
        manual_group = "shared"

    prior_usd = None
    prior_group = None
    for grp in candidate_groups:
        if grp in p_tiers and field in p_tiers[grp]:
            prior_usd = float(p_tiers[grp][field])
            prior_group = grp
            break
    if prior_usd is None and field in preset_credit:
        prior_usd = float(preset_credit[field])
        prior_group = "shared"

    pool_group = manual_group or prior_group or (
        "gemini" if agent_name == "agy" and quota_group in ("argon", "carbon", "hydrogen") else quota_group
    )
    return pool_group, prior_usd, manual_usd


def _model_pool_groups(agent_name: str, model_id: str, agent_state: dict, preset_credit: dict) -> set[str]:
    """Return all pool `quota_group`s on `agent_name` whose quota `model_id` consumes."""
    try:
        adapter = get_adapter(agent_name)
        primary, _, _ = adapter.resolve_quota_group(model_id, {"model": model_id}, agent_state)
    except Exception:
        primary = "shared"

    p_tiers = preset_credit.get("tiers") or {}
    groups = {primary}
    if "-" in primary:
        groups.add(primary.split("-", 1)[0])
    if agent_name == "agy" and primary in ("argon", "carbon", "hydrogen") and primary not in p_tiers:
        groups.add("gemini")
    if agent_name == "claude":
        groups.add("shared")
    return groups


def _extract_pool_window(agent_name: str, pool_group: str, window: str, agent_state: dict) -> dict | None:
    """Extract the raw/effective quota window dict `(used_pct, reset_at, observed_at)` for a pool."""
    tiers = agent_state.get("tiers") or {}
    if pool_group == "shared":
        win = agent_state.get(window)
        return win if isinstance(win, dict) and win.get("used_pct") is not None else None

    if window == "session_5h":
        tier_win = tiers.get(f"{pool_group}-5h")
        if isinstance(tier_win, dict) and tier_win.get("used_pct") is not None:
            return tier_win
        if agent_name == "agy" and pool_group == "gemini" and not tiers:
            top = agent_state.get("session_5h")
            if isinstance(top, dict) and top.get("used_pct") is not None:
                return top
        return None

    for key in (f"{pool_group}-weekly", pool_group):
        tier_win = tiers.get(key)
        if isinstance(tier_win, dict) and tier_win.get("used_pct") is not None:
            return tier_win
    if agent_name == "agy" and pool_group == "gemini" and not tiers:
        top = agent_state.get("weekly_7d")
        if isinstance(top, dict) and top.get("used_pct") is not None:
            return top
    return None


def _iter_preset_pools(preset_credit: dict) -> list[tuple[str, str, float]]:
    """Yield `(pool_group, window, prior_usd)` for every pool defined in `preset_credit`."""
    pools: list[tuple[str, str, float]] = []
    for win in WINDOWS:
        field = f"{win}_usd"
        if field in preset_credit:
            pools.append(("shared", win, float(preset_credit[field])))
    for grp, spec in sorted((preset_credit.get("tiers") or {}).items()):
        for win in WINDOWS:
            field = f"{win}_usd"
            if field in spec:
                pools.append((grp, win, float(spec[field])))
    return pools


def posterior_ceiling_usd(prior_usd: float, s_cost: float, s_frac: float) -> float:
    """Compute empirical Bayes ceiling `C_hat = (F0 * C_prior + S_cost) / (F0 + S_frac)`."""
    if s_frac <= 0.0:
        return float(prior_usd)
    return round((PRIOR_STRENGTH_F0 * prior_usd + s_cost) / (PRIOR_STRENGTH_F0 + s_frac), 6)


def record_usage_calibration_snapshot(
    config,
    agent_name: str,
    agent_state: dict,
    now_dt: datetime,
) -> dict | None:
    """Update empirical calibration pools for `agent_name` under `_usage_locked(config)`."""
    agent_cfg = config.agents[agent_name]
    effective_plan, preset_credit, manual_credit, _ = resolve_plan_credit_sources(
        agent_name, agent_cfg, agent_state.get("detected_plan")
    )
    if not effective_plan:
        return None
    preset_pools = _iter_preset_pools(preset_credit)
    if not preset_pools:
        return None

    cal_store = read_calibration_store(config)
    dirty = False
    telemetry_loaded = False
    obs_by_decision: dict[str, dict] = {}
    observations: list[dict] = []
    routes: list[dict] = []

    for pool_group, window, prior_usd in preset_pools:
        _, _, manual_usd = _canonical_pool_group_for_window(
            agent_name, pool_group, window, preset_credit, manual_credit
        )
        if manual_usd is not None:
            # Explicit manual `[agents.<name>].credit` overrides remain exact and uncalibrated.
            continue

        win = _extract_pool_window(agent_name, pool_group, window, agent_state)
        if win is None or win.get("used_pct") is None:
            continue

        obs_iso = (
            win.get("measured_at")
            or win.get("observed_at")
            or agent_state.get("updated_at")
            or format_timestamp(now_dt)
        )
        curr_snap = {
            "observed_at": format_timestamp(parse_timestamp(obs_iso, "calibration snapshot observed_at")),
            "used_pct": float(win["used_pct"]),
            "reset_at": (
                format_timestamp(parse_timestamp(win["reset_at"], "calibration snapshot reset_at"))
                if win.get("reset_at") is not None
                else None
            ),
        }

        pkey = make_pool_key(agent_name, effective_plan, pool_group, window)
        pool = cal_store["pools"].get(pkey)
        if pool is None:
            cal_store["pools"][pkey] = {
                "agent": agent_name,
                "plan": effective_plan,
                "quota_group": pool_group,
                "window": window,
                "s_cost": 0.0,
                "s_frac": 0.0,
                "matched_intervals": 0,
                "rejected_outliers": 0,
                "updated_at": curr_snap["observed_at"],
                "last_snapshot": curr_snap,
            }
            dirty = True
            continue

        prev_snap = pool.get("last_snapshot")
        if prev_snap is None:
            pool["last_snapshot"] = curr_snap
            dirty = True
            continue

        t_prev = parse_timestamp(prev_snap["observed_at"])
        t_curr = parse_timestamp(curr_snap["observed_at"])
        if t_curr <= t_prev:
            continue

        # Detect window reset / rollover (tolerating <600s reset_at jitter).
        rolled_over = False
        if curr_snap["used_pct"] < prev_snap["used_pct"] - 1e-6:
            rolled_over = True
        elif prev_snap["reset_at"] is not None and t_curr >= parse_timestamp(prev_snap["reset_at"]):
            rolled_over = True
        elif prev_snap["reset_at"] is not None and curr_snap["reset_at"] is not None:
            drift = abs(
                (parse_timestamp(curr_snap["reset_at"]) - parse_timestamp(prev_snap["reset_at"])).total_seconds()
            )
            if drift >= RESET_JITTER_TOLERANCE_SECONDS:
                rolled_over = True
        elif (prev_snap["reset_at"] is None) != (curr_snap["reset_at"] is None):
            rolled_over = True

        if rolled_over:
            pool["last_snapshot"] = curr_snap
            dirty = True
            continue

        delta_used_pct = curr_snap["used_pct"] - prev_snap["used_pct"]
        if delta_used_pct < MIN_DELTA_USED_PCT:
            # Keep `last_snapshot` anchored at `t_prev` so sub-0.25% steps accumulate cleanly.
            continue

        # Advance `last_snapshot` so `[t_prev, t_curr]` is evaluated at most once.
        pool["last_snapshot"] = curr_snap
        dirty = True

        if not telemetry_loaded:
            obs_by_decision, observations, routes = _collect_telemetry_sources(config)
            telemetry_loaded = True

        straddled = False
        for route in routes:
            if route["agent"] != agent_name:
                continue
            if pool_group not in _model_pool_groups(agent_name, route["model"], agent_state, preset_credit):
                continue
            r_created = route["created_dt"]
            if r_created > t_curr:
                continue
            r_obs = obs_by_decision.get(route["decision_id"]) if route["decision_id"] else None
            if r_obs is None:
                if r_created >= t_prev or (t_curr - r_created).total_seconds() <= UNOBSERVED_ROUTE_TTL_SECONDS:
                    straddled = True
                    break
            elif r_created < t_curr < r_obs["observed_dt"]:
                straddled = True
                break
        if straddled:
            continue

        interval_obs = [
            obs
            for obs in observations
            if obs["agent"] == agent_name
            and t_prev < obs["observed_dt"] <= t_curr
            and pool_group in _model_pool_groups(agent_name, obs["model"], agent_state, preset_credit)
        ]
        if not interval_obs:
            continue
        if any(obs["cost_usd"] is None or obs["cost_usd"] <= 0.0 for obs in interval_obs):
            continue

        interval_cost_usd = sum(float(obs["cost_usd"]) for obs in interval_obs)
        delta_frac = delta_used_pct / 100.0
        implied_ceiling = interval_cost_usd / delta_frac
        if not (OUTLIER_MIN_FACTOR * prior_usd <= implied_ceiling <= OUTLIER_MAX_FACTOR * prior_usd):
            pool["rejected_outliers"] = pool.get("rejected_outliers", 0) + 1
            pool["updated_at"] = curr_snap["observed_at"]
            continue

        s_cost = pool["s_cost"] + interval_cost_usd
        s_frac = pool["s_frac"] + delta_frac
        if s_frac > MAX_SAMPLE_WINDOWS_FMAX:
            decay = MAX_SAMPLE_WINDOWS_FMAX / s_frac
            s_cost *= decay
            s_frac = MAX_SAMPLE_WINDOWS_FMAX

        pool["s_cost"] = round(s_cost, 9)
        pool["s_frac"] = round(s_frac, 9)
        pool["matched_intervals"] = pool["matched_intervals"] + 1
        pool["updated_at"] = curr_snap["observed_at"]

    if dirty:
        return write_calibration_store(config, cal_store)
    return cal_store


def reset_calibration_snapshots(config, agents: list[str]) -> None:
    """Clear `last_snapshot` anchors for cleared agents while retaining learned pool priors."""
    path = calibration_path(config)
    if not path.exists():
        return
    cal_store = read_calibration_store(config)
    target_set = set(agents)
    changed = False
    for pool in cal_store["pools"].values():
        if pool["agent"] in target_set and pool.get("last_snapshot") is not None:
            pool["last_snapshot"] = None
            changed = True
    if changed:
        write_calibration_store(config, cal_store)


def resolve_calibrated_quota_credit(
    agent_name: str,
    agent_cfg: dict,
    effective_state: dict,
    quota_group: str,
    s5_win: dict,
    w7_win: dict,
    *,
    reserve_pct: float = 0.0,
    calibration_store: dict | None = None,
) -> tuple[dict | None, dict | None]:
    """Return `(credit_budget, credit_calibration_meta)` for `(agent_name, quota_group)`."""
    effective_plan, preset_credit, manual_credit, _ = resolve_plan_credit_sources(
        agent_name, agent_cfg, effective_state.get("detected_plan")
    )
    pools = (calibration_store or {}).get("pools") or {}

    windows_meta: dict[str, dict] = {}
    rem_by_win: dict[str, float | None] = {"session_5h": None, "weekly_7d": None}

    for window, win_dict in (("session_5h", s5_win), ("weekly_7d", w7_win)):
        pool_group, prior_usd, manual_usd = _canonical_pool_group_for_window(
            agent_name, quota_group, window, preset_credit, manual_credit
        )
        if manual_usd is not None:
            total_usd = manual_usd
            s_frac = 0.0
            matched = 0
            is_manual = True
        elif prior_usd is not None:
            pkey = make_pool_key(agent_name, effective_plan, pool_group, window) if effective_plan else ""
            pool = pools.get(pkey)
            s_cost = float(pool["s_cost"]) if pool else 0.0
            s_frac = float(pool["s_frac"]) if pool else 0.0
            matched = int(pool["matched_intervals"]) if pool else 0
            total_usd = posterior_ceiling_usd(prior_usd, s_cost, s_frac)
            is_manual = False
        else:
            continue

        used_pct = win_dict.get("used_pct") if isinstance(win_dict, dict) else None
        if used_pct is not None:
            pct_left = max(0.0, 100.0 - reserve_pct - float(used_pct))
            rem = round((pct_left / 100.0) * total_usd, 6)
        else:
            rem = None
        rem_by_win[window] = rem

        windows_meta[window] = {
            "pool_group": pool_group,
            "prior_total_usd": prior_usd,
            "calibrated_total_usd": total_usd,
            "effective_sample_windows": round(s_frac, 6),
            "matched_intervals": matched,
            "manual_override": is_manual,
            "used_pct": used_pct,
            "remaining_usd": rem,
        }

    if not windows_meta:
        return None, None

    present = [x for x in (rem_by_win["session_5h"], rem_by_win["weekly_7d"]) if x is not None]
    credit_budget = None
    available_usd = min(present) if present else None
    if present:
        credit_budget = {"available_usd": available_usd}
        if rem_by_win["session_5h"] is not None:
            credit_budget["session_5h_remaining_usd"] = rem_by_win["session_5h"]
        if rem_by_win["weekly_7d"] is not None:
            credit_budget["weekly_7d_remaining_usd"] = rem_by_win["weekly_7d"]

    calibration_meta = {
        "plan": effective_plan or None,
        "available_usd": available_usd,
        **windows_meta,
    }
    return credit_budget, calibration_meta


def build_agent_calibration_summary(
    agent_name: str,
    agent_cfg: dict,
    effective_state: dict,
    *,
    is_host: bool = False,
    calibration_store: dict | None = None,
) -> dict[str, dict]:
    """Build per-`quota_group` calibration summary for `agent_name`."""
    effective_plan, preset_credit, manual_credit, _ = resolve_plan_credit_sources(
        agent_name, agent_cfg, effective_state.get("detected_plan")
    )
    reserve = float(agent_cfg.get("host_reserve_pct", 0.0)) if is_host else 0.0

    groups: list[str] = []
    if any(k in preset_credit for k in ("session_5h_usd", "weekly_7d_usd")) or any(
        k in manual_credit for k in ("session_5h_usd", "weekly_7d_usd")
    ):
        groups.append("shared")
    for grp in sorted(set(preset_credit.get("tiers") or {}) | set(manual_credit.get("tiers") or {})):
        if grp not in groups:
            groups.append(grp)

    out: dict[str, dict] = {}
    tiers = effective_state.get("tiers") or {}
    for grp in groups:
        if grp == "shared":
            s5 = effective_state.get("session_5h") or {"used_pct": None, "reset_at": None}
            w7 = effective_state.get("weekly_7d") or {"used_pct": None, "reset_at": None}
        else:
            s5 = tiers.get(f"{grp}-5h") or (
                effective_state.get("session_5h")
                if agent_name == "agy" and grp == "gemini" and not tiers
                else {"used_pct": None, "reset_at": None}
            )
            w7 = (
                tiers.get(f"{grp}-weekly")
                or tiers.get(grp)
                or (
                    effective_state.get("weekly_7d")
                    if agent_name == "agy" and grp == "gemini" and not tiers
                    else {"used_pct": None, "reset_at": None}
                )
            )
        _, meta = resolve_calibrated_quota_credit(
            agent_name,
            agent_cfg,
            effective_state,
            grp,
            s5,
            w7,
            reserve_pct=reserve,
            calibration_store=calibration_store,
        )
        if meta is not None:
            out[grp] = meta
    return out

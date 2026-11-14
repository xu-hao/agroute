"""Durable model-route log, howhard decision correlation, and TUI/CLI snapshot builder."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .files import append_jsonl, atomic_write
from .state import claim
from .usage import _now_utc, _usage_locked
from .validation import AGENTS, AgRouteError, format_timestamp

SNAPSHOT_FORMAT = "agroute-snapshot-v1"
REPOS_FORMAT = "agroute-repositories-v1"
OBSERVED_STATUSES = frozenset({"clean", "loud", "silent"})

_JSON_CACHE: dict[str, tuple[int, int, object]] = {}


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _read_cached_json(path: Path) -> dict | None:
    key = str(path)
    sig = _stat_key(path)
    if sig is None:
        _JSON_CACHE.pop(key, None)
        return None
    cached = _JSON_CACHE.get(key)
    if cached is not None and cached[:2] == sig:
        return cached[2]  # type: ignore[return-value]
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    _JSON_CACHE[key] = (sig[0], sig[1], data)
    return data


def _read_cached_jsonl(path: Path) -> list[dict]:
    key = str(path)
    sig = _stat_key(path)
    if sig is None:
        _JSON_CACHE.pop(key, None)
        return []
    cached = _JSON_CACHE.get(key)
    if cached is not None and cached[:2] == sig:
        return cached[2]  # type: ignore[return-value]
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    _JSON_CACHE[key] = (sig[0], sig[1], rows)
    return rows


def infer_agent_from_model(raw_model: str) -> tuple[str, str]:
    """Return (agent, bare_model) from either a provider-qualified or native model ID."""
    value = (raw_model or "").strip()
    for sep in ("/", ":"):
        if sep in value:
            prefix, rest = value.split(sep, 1)
            if prefix in AGENTS and rest:
                return prefix, rest
    lower = value.lower()
    if lower.startswith("claude-") or lower in ("opus", "sonnet", "haiku", "fable"):
        return "claude", value
    if lower.startswith("gemini-") or lower in ("pro", "flash", "flash_lite", "inherit"):
        return "agy", value
    return "codex", value


def _first_line(text: str | None, fallback: str = "task") -> str:
    if not text or not isinstance(text, str):
        return fallback
    for line in text.splitlines():
        cleaned = line.strip()
        while cleaned.startswith(("# ", "## ", "### ", "- ", "* ")):
            cleaned = cleaned.split(" ", 1)[1].strip()
        stripped = " ".join(cleaned.split())
        if stripped:
            return stripped[:160]
    return fallback


def _normalize_iso_utc(raw: str | None) -> str:
    if not raw or not isinstance(raw, str):
        return format_timestamp(datetime.now(timezone.utc))
    candidate = raw.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return format_timestamp(dt)


def _spool_dir() -> Path | None:
    if os.environ.get("AGROUTE_DISABLE_SPOOL"):
        return None
    env = os.environ.get("AGROUTE_SPOOL_DIR")
    if env:
        return Path(env).expanduser().resolve()
    xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return (xdg / "agroute").resolve()


def register_repository(config) -> None:
    """Register a repository in the machine-local spool when not using an isolated state dir."""
    if getattr(config, "explicit_state", False):
        return
    spool = _spool_dir()
    if spool is None:
        return
    try:
        spool.mkdir(parents=True, exist_ok=True)
        lock_path = spool / "repositories.lock"
        reg_path = spool / "repositories.json"
        repo_id = config.project_identity or config.capture_root.name
        entry = {
            "capture_root": str(config.capture_root),
            "state_root": str(config.state_root),
            "repository_id": repo_id,
        }
        with lock_path.open("a+b") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                existing: list[dict] = []
                if reg_path.exists():
                    try:
                        parsed = json.loads(reg_path.read_text(encoding="utf-8"))
                        if isinstance(parsed, dict) and isinstance(parsed.get("repositories"), list):
                            existing = [r for r in parsed["repositories"] if isinstance(r, dict)]
                    except (OSError, ValueError):
                        existing = []
                by_state = {r.get("state_root"): r for r in existing if r.get("state_root")}
                if by_state.get(entry["state_root"]) != entry:
                    by_state[entry["state_root"]] = entry
                    ordered = sorted(by_state.values(), key=lambda r: str(r.get("capture_root", "")))
                    atomic_write(
                        reg_path,
                        json.dumps({"format": REPOS_FORMAT, "repositories": ordered}, indent=2, sort_keys=True) + "\n",
                    )
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        pass


def registered_repositories(config) -> list[dict]:
    """Return repositories to inspect (strictly current state_root when explicit_state is True)."""
    current_id = config.project_identity or config.capture_root.name
    current = {
        "capture_root": str(config.capture_root),
        "state_root": str(config.state_root),
        "repository_id": current_id,
    }
    if getattr(config, "explicit_state", False):
        return [current]
    register_repository(config)
    spool = _spool_dir()
    if spool is None:
        return [current]
    reg_path = spool / "repositories.json"
    data = _read_cached_json(reg_path)
    if not data or not isinstance(data.get("repositories"), list):
        return [current]
    by_state = {current["state_root"]: current}
    for item in data["repositories"]:
        if not isinstance(item, dict):
            continue
        sr = item.get("state_root")
        cr = item.get("capture_root")
        if isinstance(sr, str) and isinstance(cr, str) and Path(cr).exists():
            by_state[sr] = {
                "capture_root": cr,
                "state_root": sr,
                "repository_id": str(item.get("repository_id") or Path(cr).name),
            }
    return list(by_state.values())


def record_plan_route(config, plan: dict) -> str:
    """Persist an agr plan execution record and return its route_id."""
    seed = (
        f"{plan['evaluated_at']}:{plan['decision_id']}:{plan['host_agent']}:"
        f"{plan['target_agent']}:{plan['resolved_target']['model']}:{plan['resolved_target']['effort']}"
    )
    route_id = "route-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    record = {
        "format": "agroute-route-v1",
        "route_id": route_id,
        "created_at": plan["evaluated_at"],
        "repository": str(config.capture_root),
        "repository_id": config.project_identity or config.capture_root.name,
        "host_agent": plan["host_agent"],
        "target_agent": plan["target_agent"],
        "execution_mode": plan["execution_mode"],
        "decision_id": plan["decision_id"],
        "cap_met": plan.get("cap_met"),
        "needs_review": bool(plan.get("needs_review", False)),
        "host_affinity_applied": bool(plan.get("host_affinity_applied", False)),
        "host_affinity_detail": plan.get("host_affinity_detail"),
        "howhard_cell": deepcopy(plan["howhard_cell"]),
        "resolved_target": deepcopy(plan["resolved_target"]),
        "role": (
            plan["invocation"].get("parameters", {}).get("Role")
            or plan["invocation"].get("parameters", {}).get("description")
            or "Task Worker"
        ),
        "invocation": deepcopy(plan["invocation"]),
        "feedback": deepcopy(plan["feedback"]),
    }
    if "credit_budget" in plan and plan["credit_budget"] is not None:
        record["credit_budget"] = deepcopy(plan["credit_budget"])
    if "needed_credit_usd" in plan and plan["needed_credit_usd"] is not None:
        record["needed_credit_usd"] = plan["needed_credit_usd"]
    for key in ("session_cost_usd", "cost_per_credit", "monthly_cost_usd", "weekly_cost_usd"):
        if plan.get(key) is not None:
            record[key] = plan[key]
    with _usage_locked(config):
        claim(config)
        record["repository_id"] = config.project_identity or config.capture_root.name
        append_jsonl(config.state_root / "routes.jsonl", record)
    register_repository(config)
    return route_id


def _extract_forecasts(decision_doc: dict | None) -> list[dict]:
    if not isinstance(decision_doc, dict):
        return []
    semantic = decision_doc.get("semantic")
    if isinstance(semantic, dict):
        final = semantic.get("final")
        if isinstance(final, dict) and isinstance(final.get("forecasts"), list):
            return [f for f in final["forecasts"] if isinstance(f, dict)]
    for key in ("candidates", "cells", "ranking"):
        if isinstance(decision_doc.get(key), list):
            return [f for f in decision_doc[key] if isinstance(f, dict)]
    return []


def _load_howhard_observations(hh_root: Path) -> tuple[dict[str, dict], list[dict]]:
    pending = _read_cached_jsonl(hh_root / "pending.jsonl")
    if not pending:
        return {}, []
    by_id: dict[str, dict] = {}
    ordered_ids: list[str] = []
    for item in pending:
        obs_id = item.get("id")
        if not isinstance(obs_id, str) or not obs_id:
            continue
        amends = item.get("amends")
        if isinstance(amends, str) and amends in by_id:
            prior = by_id[amends]
            merged = {**prior, **item}
            if not merged.get("decision_id") and prior.get("decision_id"):
                merged["decision_id"] = prior["decision_id"]
            by_id[amends] = merged
            by_id[obs_id] = merged
        else:
            by_id[obs_id] = item
            ordered_ids.append(obs_id)

    obs_by_decision: dict[str, dict] = {}
    raw_observations: list[dict] = []
    seen_final: set[int] = set()
    for obs_id in ordered_ids:
        final_obs = by_id[obs_id]
        if id(final_obs) in seen_final:
            continue
        seen_final.add(id(final_obs))
        dec_id = final_obs.get("decision_id")
        if isinstance(dec_id, str) and dec_id:
            obs_by_decision[dec_id] = final_obs
        else:
            raw_observations.append(final_obs)
    return obs_by_decision, raw_observations


def load_repository_routes(capture_root: Path, state_root: Path, repository_id: str) -> list[dict]:
    """Load and correlate agr plan records with howhard decisions and outcomes."""
    plan_records = _read_cached_jsonl(state_root / "routes.jsonl")
    hh_root = state_root.parent / "howhard"
    if not hh_root.is_dir():
        alt = capture_root / ".git" / "howhard"
        if alt.is_dir():
            hh_root = alt

    obs_by_decision: dict[str, dict] = {}
    raw_observations: list[dict] = []
    hh_events: list[dict] = []
    if hh_root.is_dir():
        obs_by_decision, raw_observations = _load_howhard_observations(hh_root)
        hh_events = _read_cached_jsonl(hh_root / "decision-events.jsonl")

    routes: list[dict] = []
    covered_decisions: set[str] = set()

    for rec in plan_records:
        route_id = str(rec.get("route_id") or "")
        if not route_id:
            continue
        dec_id = rec.get("decision_id") if isinstance(rec.get("decision_id"), str) else None
        if dec_id:
            covered_decisions.add(dec_id)

        dec_doc = _read_cached_json(hh_root / "decisions" / f"{dec_id}.json") if (dec_id and hh_root.is_dir()) else None
        obs = obs_by_decision.get(dec_id) if dec_id else None

        resolved = rec.get("resolved_target") if isinstance(rec.get("resolved_target"), dict) else {}
        host_agent = str(rec.get("host_agent") or "agy")
        target_agent = str(rec.get("target_agent") or resolved.get("agent") or host_agent)
        model = str(resolved.get("model") or "unknown")
        effort = str(resolved.get("effort") or "standard")
        qualified = str((rec.get("howhard_cell") or {}).get("model") or f"{target_agent}/{model}")
        needs_review = bool(rec.get("needs_review", False))
        cap_met = rec.get("cap_met")

        outcome = obs.get("outcome") if isinstance(obs, dict) and obs.get("outcome") in OBSERVED_STATUSES else None
        if outcome:
            status = str(outcome)
        elif needs_review or cap_met is False:
            status = "review"
        else:
            status = "routed"

        task_text = None
        if isinstance(dec_doc, dict):
            ctx = dec_doc.get("context")
            if isinstance(ctx, dict) and isinstance(ctx.get("task_text"), str):
                task_text = ctx["task_text"]
        if not task_text and isinstance(obs, dict) and isinstance(obs.get("task_text"), str):
            task_text = obs["task_text"]

        role = str(rec.get("role") or "Task Worker")
        label = _first_line(task_text, fallback=role if role != "Task Worker" else (obs.get("id") if obs else role))
        task_type = str(
            (dec_doc or {}).get("task_type")
            or (obs or {}).get("task_type")
            or "task"
        )
        risk_class = (dec_doc or {}).get("risk_class") or (obs or {}).get("risk_class")

        route_entry = {
            "route_id": route_id,
            "decision_id": dec_id,
            "status": status,
            "execution_mode": str(rec.get("execution_mode") or ("native" if host_agent == target_agent else "cli")),
            "repository": str(rec.get("repository") or capture_root),
            "repository_id": str(rec.get("repository_id") or repository_id),
            "host_agent": host_agent,
            "target_agent": target_agent,
            "model": model,
            "qualified_model": qualified,
            "effort": effort,
            "task_type": task_type,
            "risk_class": str(risk_class) if risk_class else None,
            "role": role,
            "label": label,
            "cap_met": cap_met if isinstance(cap_met, bool) else None,
            "needs_review": needs_review,
            "host_affinity_applied": bool(rec.get("host_affinity_applied", False)),
            "created_at": _normalize_iso_utc(rec.get("created_at")),
            "observed_at": _normalize_iso_utc(obs.get("observed_at")) if isinstance(obs, dict) and obs.get("observed_at") else None,
            "outcome": outcome,
            "execution_id": str(obs.get("id")) if isinstance(obs, dict) and obs.get("id") else None,
            "tokens": obs.get("tokens") if isinstance(obs, dict) and isinstance(obs.get("tokens"), int) else None,
            "seconds": float(obs["seconds"]) if isinstance(obs, dict) and type(obs.get("seconds")) in (int, float) else None,
            "cost_usd": float(obs["cost_usd"]) if isinstance(obs, dict) and type(obs.get("cost_usd")) in (int, float) else None,
            "invocation": rec.get("invocation") if isinstance(rec.get("invocation"), dict) else None,
            "forecasts": _extract_forecasts(dec_doc),
            "note": str(obs.get("note")) if isinstance(obs, dict) and obs.get("note") else None,
            "task_text": task_text[:4000] if isinstance(task_text, str) else None,
        }
        if isinstance(rec.get("credit_budget"), dict):
            route_entry["credit_budget"] = deepcopy(rec["credit_budget"])
        if type(rec.get("needed_credit_usd")) in (int, float):
            route_entry["needed_credit_usd"] = float(rec["needed_credit_usd"])
        for key in ("session_cost_usd", "cost_per_credit", "monthly_cost_usd", "weekly_cost_usd"):
            if type(rec.get(key)) in (int, float):
                route_entry[key] = float(rec[key])
        routes.append(route_entry)

    for ev in hh_events:
        dec_id = ev.get("decision_id")
        if not isinstance(dec_id, str) or not dec_id or dec_id in covered_decisions:
            continue
        covered_decisions.add(dec_id)
        dec_doc = _read_cached_json(hh_root / "decisions" / f"{dec_id}.json")
        obs = obs_by_decision.get(dec_id)
        chosen = (dec_doc or {}).get("chosen") if isinstance(dec_doc, dict) else None
        raw_model = str(
            (chosen or {}).get("model")
            or (obs or {}).get("model")
            or "unknown"
        )
        effort = str(
            (chosen or {}).get("effort")
            or (obs or {}).get("effort")
            or "standard"
        )
        target_agent, bare_model = infer_agent_from_model(raw_model)
        outcome = obs.get("outcome") if isinstance(obs, dict) and obs.get("outcome") in OBSERVED_STATUSES else None
        needs_review = bool((dec_doc or {}).get("needs_review", False))
        cap_met = (dec_doc or {}).get("cap_met")
        if outcome:
            status = str(outcome)
        elif needs_review or cap_met is False:
            status = "review"
        else:
            status = "routed"

        task_text = None
        if isinstance(dec_doc, dict):
            ctx = dec_doc.get("context")
            if isinstance(ctx, dict) and isinstance(ctx.get("task_text"), str):
                task_text = ctx["task_text"]
        if not task_text and isinstance(obs, dict) and isinstance(obs.get("task_text"), str):
            task_text = obs["task_text"]

        task_type = str(
            (dec_doc or {}).get("task_type")
            or (obs or {}).get("task_type")
            or ev.get("task_type")
            or "task"
        )
        risk_class = (dec_doc or {}).get("risk_class") or (obs or {}).get("risk_class")
        fallback_label = str(obs.get("id")) if isinstance(obs, dict) and obs.get("id") else task_type
        label = _first_line(task_text, fallback=fallback_label)

        routes.append(
            {
                "route_id": f"hh-{dec_id[:12]}",
                "decision_id": dec_id,
                "status": status,
                "execution_mode": "native",
                "repository": str(capture_root),
                "repository_id": repository_id,
                "host_agent": target_agent,
                "target_agent": target_agent,
                "model": bare_model,
                "qualified_model": f"{target_agent}/{bare_model}",
                "effort": effort,
                "task_type": task_type,
                "risk_class": str(risk_class) if risk_class else None,
                "role": None,
                "label": label,
                "cap_met": cap_met if isinstance(cap_met, bool) else None,
                "needs_review": needs_review,
                "host_affinity_applied": False,
                "created_at": _normalize_iso_utc(ev.get("at_utc")),
                "observed_at": _normalize_iso_utc(obs.get("observed_at")) if isinstance(obs, dict) and obs.get("observed_at") else None,
                "outcome": outcome,
                "execution_id": str(obs.get("id")) if isinstance(obs, dict) and obs.get("id") else None,
                "tokens": obs.get("tokens") if isinstance(obs, dict) and isinstance(obs.get("tokens"), int) else None,
                "seconds": float(obs["seconds"]) if isinstance(obs, dict) and type(obs.get("seconds")) in (int, float) else None,
                "cost_usd": float(obs["cost_usd"]) if isinstance(obs, dict) and type(obs.get("cost_usd")) in (int, float) else None,
                "invocation": None,
                "forecasts": _extract_forecasts(dec_doc),
                "note": str(obs.get("note")) if isinstance(obs, dict) and obs.get("note") else None,
                "task_text": task_text[:4000] if isinstance(task_text, str) else None,
            }
        )

    for obs in raw_observations:
        obs_id = str(obs.get("id") or "")
        if not obs_id:
            continue
        raw_model = str(obs.get("model") or "unknown")
        effort = str(obs.get("effort") or "standard")
        target_agent, bare_model = infer_agent_from_model(raw_model)
        outcome = str(obs.get("outcome")) if obs.get("outcome") in OBSERVED_STATUSES else "routed"
        task_text = obs.get("task_text") if isinstance(obs.get("task_text"), str) else None
        task_type = str(obs.get("task_type") or "task")
        label = _first_line(task_text, fallback=obs_id)
        short_hash = hashlib.sha256(obs_id.encode("utf-8")).hexdigest()[:12]
        routes.append(
            {
                "route_id": f"obs-{short_hash}",
                "decision_id": None,
                "status": outcome,
                "execution_mode": "raw",
                "repository": str(capture_root),
                "repository_id": repository_id,
                "host_agent": target_agent,
                "target_agent": target_agent,
                "model": bare_model,
                "qualified_model": f"{target_agent}/{bare_model}",
                "effort": effort,
                "task_type": task_type,
                "risk_class": str(obs["risk_class"]) if obs.get("risk_class") else None,
                "role": None,
                "label": label,
                "cap_met": None,
                "needs_review": False,
                "host_affinity_applied": False,
                "created_at": _normalize_iso_utc(obs.get("observed_at")),
                "observed_at": _normalize_iso_utc(obs.get("observed_at")),
                "outcome": outcome if outcome in OBSERVED_STATUSES else None,
                "execution_id": obs_id,
                "tokens": obs.get("tokens") if isinstance(obs.get("tokens"), int) else None,
                "seconds": float(obs["seconds"]) if type(obs.get("seconds")) in (int, float) else None,
                "cost_usd": float(obs["cost_usd"]) if type(obs.get("cost_usd")) in (int, float) else None,
                "invocation": None,
                "forecasts": [],
                "note": str(obs.get("note")) if obs.get("note") else None,
                "task_text": task_text[:4000] if isinstance(task_text, str) else None,
            }
        )

    return routes


def build_snapshot(router, *, all_repos: bool = True, now=None) -> dict:
    """Build a complete usage + routes snapshot for the TUI and CLI."""
    now_dt = _now_utc(now)
    usage = router.usage_show(now=now)
    repos = registered_repositories(router.config) if all_repos else [
        {
            "capture_root": str(router.config.capture_root),
            "state_root": str(router.config.state_root),
            "repository_id": router.config.project_identity or router.config.capture_root.name,
        }
    ]
    all_routes: list[dict] = []
    for repo in repos:
        all_routes.extend(
            load_repository_routes(
                Path(repo["capture_root"]),
                Path(repo["state_root"]),
                str(repo["repository_id"]),
            )
        )
    all_routes.sort(key=lambda r: (r["created_at"], r["route_id"]), reverse=True)
    routed_count = sum(1 for r in all_routes if r["status"] in ("routed", "review"))
    observed_count = sum(1 for r in all_routes if r["status"] in OBSERVED_STATUSES)
    return {
        "format": SNAPSHOT_FORMAT,
        "captured_at": format_timestamp(now_dt),
        "default_host": router.config.default_host,
        "agents": usage["agents"],
        "routed_count": routed_count,
        "observed_count": observed_count,
        "routes": all_routes,
    }


def find_route(snapshot: dict, identifier: str) -> dict:
    """Resolve one route by exact or prefix match on route_id, decision_id, or execution_id."""
    needle = (identifier or "").strip()
    if not needle:
        raise AgRouteError("show: route identifier must be non-empty")
    routes = snapshot.get("routes") or []
    exact = [
        r
        for r in routes
        if needle in (r.get("route_id"), r.get("decision_id"), r.get("execution_id"))
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise AgRouteError(f"show: identifier '{needle}' matched multiple routes")
    prefix = [
        r
        for r in routes
        if (isinstance(r.get("route_id"), str) and r["route_id"].startswith(needle))
        or (isinstance(r.get("decision_id"), str) and r["decision_id"].startswith(needle))
        or (isinstance(r.get("execution_id"), str) and r["execution_id"].startswith(needle))
    ]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        raise AgRouteError(f"show: prefix '{needle}' is ambiguous ({len(prefix)} matches)")
    raise AgRouteError(f"show: unknown route '{needle}'")

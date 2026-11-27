"""Durable model-route log, howhard decision correlation, and TUI/CLI snapshot builder."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .files import atomic_write
from .price_store import planned_cell_price
from .route_store import (
    append_route_records,
    decision_summary,
    howhard_payload_decisions,
    read_route_records,
    writable_store,
)
from .state import claim
from .usage import _now_utc, _usage_locked, reconcile_reservation_on_close_locked
from .validation import AGENTS, AgRouteError, format_timestamp, number, text

SNAPSHOT_FORMAT = "agroute-snapshot-v1"
REPOS_FORMAT = "agroute-repositories-v1"
ROUTE_CLOSE_FORMAT = "agroute-route-close-v1"
OBSERVED_STATUSES = frozenset({"clean", "loud", "silent", "interrupted"})
CLOSE_STATUSES = ("done", "switched", "cancelled")
TERMINAL_ROUTE_STATUSES = frozenset(CLOSE_STATUSES)

_JSON_CACHE: dict[str, tuple[int, int, object]] = {}


def _stat_key(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _read_cached_json(path: Path) -> dict | None:
    """Cache small registry documents; receipts use SQLite decision summaries."""
    key = str(path)
    sig = _stat_key(path)
    if sig is None:
        _JSON_CACHE.pop(key, None)
        return None
    cached = _JSON_CACHE.get(key)
    if cached is not None and cached[:2] == sig:
        return cached[2]
    try:
        data = json.loads(path.read_bytes())
    except (OSError, ValueError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    _JSON_CACHE[key] = (*sig, data)
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
        if isinstance(sr, str) and isinstance(cr, str) and (Path(cr).exists() or Path(sr).exists()):
            by_state[sr] = {
                "capture_root": cr,
                "state_root": sr,
                "repository_id": str(item.get("repository_id") or Path(cr).name),
            }
    return list(by_state.values())


def mint_route_id(
    evaluated_at: str,
    decision_id: str | None,
    host_agent: str,
    target_agent: str,
    model: str,
    effort: str,
) -> str:
    """Compute the deterministic `route-<12hex>` ID for a planned or switched route."""
    seed = f"{evaluated_at}:{decision_id or 'switched'}:{host_agent}:{target_agent}:{model}:{effort}"
    return "route-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


def record_plan_route(
    config,
    plan: dict,
    *,
    locked: bool = False,
    route_id: str | None = None,
    task_text: str | None = None,
) -> str:
    """Persist an agr plan execution record and return its route_id."""
    if route_id is None:
        route_id = mint_route_id(
            plan["evaluated_at"],
            plan["decision_id"],
            plan["host_agent"],
            plan["target_agent"],
            plan["resolved_target"]["model"],
            plan["resolved_target"]["effort"],
        )
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
    if isinstance(task_text, str) and task_text.strip():
        record["task_text"] = task_text[:4000]
    if "credit_budget" in plan and plan["credit_budget"] is not None:
        record["credit_budget"] = deepcopy(plan["credit_budget"])
    if "needed_credit_usd" in plan and plan["needed_credit_usd"] is not None:
        record["needed_credit_usd"] = plan["needed_credit_usd"]
    for key in ("credit_p80_usd", "session_cost_usd", "cost_per_credit", "monthly_cost_usd", "weekly_cost_usd"):
        if plan.get(key) is not None:
            record[key] = plan[key]
    for key in ("rate_card", "usd_per_mtok"):
        if plan.get(key) is not None:
            record[key] = deepcopy(plan[key])
    if locked:
        claim(config)
        record["repository_id"] = config.project_identity or config.capture_root.name
        append_route_records(config.state_root, [record])
    else:
        with _usage_locked(config):
            claim(config)
            record["repository_id"] = config.project_identity or config.capture_root.name
            append_route_records(config.state_root, [record])
    register_repository(config)
    return route_id


def _extract_forecasts(decision_doc: dict | None) -> list[dict]:
    if not isinstance(decision_doc, dict):
        return []
    if isinstance(decision_doc.get("forecasts"), list):
        return decision_doc["forecasts"]
    selection = decision_doc.get("selection")
    if isinstance(selection, dict) and isinstance(selection.get("cells"), list) and selection["cells"]:
        return [f for f in selection["cells"] if isinstance(f, dict)]
    semantic = decision_doc.get("semantic")
    if isinstance(semantic, dict):
        final = semantic.get("final")
        if isinstance(final, dict) and isinstance(final.get("forecasts"), list):
            return [f for f in final["forecasts"] if isinstance(f, dict)]
        worlds = semantic.get("worlds")
        if isinstance(worlds, dict) and isinstance(worlds.get("llm"), list):
            return [f for f in worlds["llm"] if isinstance(f, dict)]
    for key in ("candidates", "cells", "ranking"):
        if isinstance(decision_doc.get(key), list):
            return [f for f in decision_doc[key] if isinstance(f, dict)]
    return []


def _decision_selection_flag(decision_doc: dict | None, key: str, default=None):
    if not isinstance(decision_doc, dict):
        return default
    if decision_doc.get(key) is not None:
        return decision_doc[key]
    selection = decision_doc.get("selection")
    if isinstance(selection, dict) and selection.get(key) is not None:
        return selection[key]
    return default


def _chosen_selection_cell(decision_doc: dict | None, chosen: dict | None) -> dict | None:
    if not isinstance(decision_doc, dict) or not isinstance(chosen, dict):
        return None
    target_model = chosen.get("model")
    target_effort = chosen.get("effort")
    if not isinstance(target_model, str) or not isinstance(target_effort, str):
        return None
    selection = decision_doc.get("selection")
    panels = []
    if isinstance(selection, dict) and isinstance(selection.get("cells"), list):
        panels.append(selection["cells"])
    for key in ("cells", "candidates", "ranking"):
        if isinstance(decision_doc.get(key), list):
            panels.append(decision_doc[key])
    for panel in panels:
        for item in panel:
            if isinstance(item, dict) and item.get("model") == target_model and item.get("effort") == target_effort:
                return item
    return None


def _summarize_decision(document: dict) -> dict:
    """Keep only display metadata; never retain the multi-MB semantic payload."""
    scalar_fields = (
        "model", "family", "effort", "clean", "loud", "silent", "tokens", "seconds",
        "C_run", "session_cost_usd", "needed_credit_usd", "cost_per_credit", "J",
        "cap_met", "needs_review", "available_credit_usd", "credit_ok",
        "agent", "credit_p80_usd", "tier", "pi",
    )

    def cell_summary(cell):
        result = {key: cell[key] for key in scalar_fields
                  if isinstance(cell.get(key), (str, int, float, bool))}
        for key in ("P_clean", "P_loud", "P_silent", "credit_budget"):
            value = cell.get(key)
            if isinstance(value, dict):
                result[key] = {k: v for k, v in value.items() if isinstance(v, (int, float, bool))}
        return result

    chosen = document.get("chosen")
    chosen = chosen if isinstance(chosen, dict) else {}
    chosen_cell = _chosen_selection_cell(document, chosen)
    context = document.get("context")
    task_text = context.get("task_text") if isinstance(context, dict) else None
    summary = {
        "task_type": document.get("task_type"),
        "risk_class": document.get("risk_class"),
        "cap_met": _decision_selection_flag(document, "cap_met"),
        "needs_review": _decision_selection_flag(document, "needs_review", False),
        "chosen": cell_summary(chosen),
        "context": {"task_text": task_text[:4000] if isinstance(task_text, str) else None},
        "cells": [cell_summary(chosen_cell)] if chosen_cell is not None else [],
        "forecasts": [cell_summary(cell) for cell in _extract_forecasts(document)[:8]],
    }
    if isinstance(document.get("host"), str):
        summary["host"] = document["host"]
    return summary


def _load_howhard_observations(hh_root: Path) -> tuple[dict[str, dict], list[dict]]:
    entries = list(_read_cached_jsonl(hh_root / "outcomes.jsonl"))
    entries.extend(_read_cached_jsonl(hh_root / "pending.jsonl"))
    if not entries:
        return {}, []
    by_id: dict[str, dict] = {}
    ordered_ids: list[str] = []
    for item in entries:
        obs_id = item.get("id")
        if not isinstance(obs_id, str) or not obs_id:
            continue
        amends = item.get("amends")
        if isinstance(amends, str) and amends in by_id:
            prior = by_id[amends]
            merged = dict(prior)
            for field in ("outcome", "cause", "note", "observed_at"):
                if field in item:
                    merged[field] = item[field]
            for field in ("tokens", "seconds", "cost_usd"):
                if item.get(field) is not None:
                    merged[field] = item[field]
            corr = item.get("measurement_corrections")
            if isinstance(corr, dict):
                for field in ("tokens", "seconds", "cost_usd"):
                    if field in corr:
                        merged[field] = corr[field]
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


def _resolve_howhard_root(capture_root: Path, state_root: Path) -> Path | None:
    for cand in (state_root.parent / "howhard", capture_root / ".git" / "howhard", capture_root / ".howhard"):
        if cand.is_dir():
            return cand
    return None


def close_plan_route(
    config,
    identifier: str,
    *,
    status: str | None = None,
    obs: str | None = None,
    switched_from: str | None = None,
    superseded_by: str | None = None,
    switched_to: str | None = None,
    effort: str | None = None,
    tokens: int | None = None,
    seconds: float | None = None,
    credit_usd: float | None = None,
    note: str | None = None,
    now=None,
) -> dict:
    """Append an `agroute-route-close-v1` record (and optional switched continuation route) in `routes.sqlite3`."""
    needle = text(identifier, "route_id", max_length=128).strip()
    if status is None:
        close_status = "switched" if (switched_to is not None or superseded_by is not None) else "done"
    else:
        close_status = text(status, "status", label=True, max_length=32)
    if close_status not in CLOSE_STATUSES:
        raise AgRouteError(f"close: status must be one of {list(CLOSE_STATUSES)}, got {close_status!r}")

    obs_id = text(obs, "obs", max_length=256).strip() if obs is not None else None
    if obs_id is not None and close_status == "cancelled":
        raise AgRouteError("close: --obs cannot be combined with --status cancelled")
    if switched_to is not None and close_status != "switched":
        raise AgRouteError("close: --switched-to requires --status switched (or omitted --status)")

    from_id = text(switched_from, "switched_from", max_length=128).strip() if switched_from is not None else None
    sup_id = text(superseded_by, "superseded_by", max_length=128).strip() if superseded_by is not None else None
    note_txt = text(note, "note", max_length=2000) if note is not None else None

    if tokens is not None:
        if not isinstance(tokens, int) or isinstance(tokens, bool) or tokens < 0:
            raise AgRouteError("close: tokens must be a non-negative integer")
    sec_val = float(number(seconds, "seconds", nonnegative=True)) if seconds is not None else None
    cred_val = float(number(credit_usd, "credit_usd", nonnegative=True)) if credit_usd is not None else None

    now_dt = _now_utc(now)
    closed_iso = format_timestamp(now_dt)
    routes_path = config.state_root / "routes.sqlite3"
    raw_records = read_route_records(config.state_root)
    plan_by_id: dict[str, dict] = {}
    for rec in raw_records:
        if rec.get("format") == ROUTE_CLOSE_FORMAT:
            continue
        rid = rec.get("route_id")
        if isinstance(rid, str) and rid:
            plan_by_id[rid] = rec

    if needle in plan_by_id:
        target_rec = plan_by_id[needle]
    else:
        matches = [r for rid, r in plan_by_id.items() if rid.startswith(needle)]
        if len(matches) == 1:
            target_rec = matches[0]
        elif len(matches) > 1:
            raise AgRouteError(f"close: route prefix '{needle}' is ambiguous ({len(matches)} matches)")
        else:
            raise AgRouteError(f"close: unknown planned route '{needle}' in {routes_path}")

    resolved_route_id = target_rec["route_id"]

    linked_obs_credit: float | None = None
    if obs_id is not None:
        hh_root = _resolve_howhard_root(config.capture_root, config.state_root)
        if hh_root is None:
            raise AgRouteError(f"close: howhard store not found while looking up --obs '{obs_id}'")
        obs_by_dec, raw_list = _load_howhard_observations(hh_root)
        raw_map = {str(o.get("id")): o for o in raw_list if o.get("id")}
        if obs_id not in raw_map:
            if any(str(o.get("id")) == obs_id for o in obs_by_dec.values()):
                raise AgRouteError(
                    f"close: observation '{obs_id}' already carries a howhard decision_id and links automatically"
                )
            raise AgRouteError(f"close: unknown standalone howhard observation '{obs_id}'")
        matched_raw = raw_map[obs_id]
        for k in ("needed_credit_usd", "credit_usd", "cost_usd"):
            if type(matched_raw.get(k)) in (int, float):
                linked_obs_credit = float(matched_raw[k])
                break

    continuation_record: dict | None = None
    continuation_route_id: str | None = None
    price_warning: str | None = None
    if switched_to is not None:
        raw_sw = text(switched_to, "switched_to", max_length=200).strip()
        if "/" in raw_sw or ":" in raw_sw:
            sep = "/" if "/" in raw_sw else ":"
            prefix, rest = raw_sw.split(sep, 1)
            if prefix not in AGENTS or not rest.strip():
                raise AgRouteError(f"close: invalid --switched-to model '{raw_sw}'")
            cont_agent, cont_model = prefix, rest.strip()
        else:
            cont_agent, cont_model = infer_agent_from_model(raw_sw)
        prev_target = target_rec.get("resolved_target") or {}
        cont_effort = (
            text(effort, "effort", label=True, max_length=32)
            if effort is not None
            else str(prev_target.get("effort") or "high")
        )
        host_agent = str(target_rec.get("host_agent") or "agy")
        cont_mode = "native" if cont_agent == host_agent else "cli"
        continuation_route_id = mint_route_id(
            closed_iso,
            f"switch:{resolved_route_id}",
            host_agent,
            cont_agent,
            cont_model,
            cont_effort,
        )
        sup_id = continuation_route_id
        qualified_cont = f"{cont_agent}{config.namespace_separator}{cont_model}"
        configured_cell = next(
            (
                c
                for c in config.agents[cont_agent][f"{cont_mode}_candidates"]
                if c["model"] == cont_model and c["effort"] == cont_effort
            ),
            {"model": cont_model, "effort": cont_effort},
        )
        # Offline by design: closing a route never probes usage or discovers models.
        price_fields, price_warning = planned_cell_price(
            config, cont_agent, configured_cell, cont_mode, qualified_cont, now_dt
        )
        continuation_record = {
            "format": "agroute-route-v1",
            "route_id": continuation_route_id,
            "created_at": closed_iso,
            "repository": str(target_rec.get("repository") or config.capture_root),
            "repository_id": config.project_identity or config.capture_root.name,
            "host_agent": host_agent,
            "target_agent": cont_agent,
            "execution_mode": cont_mode,
            "decision_id": None,
            "switched_from": resolved_route_id,
            "cap_met": None,
            "needs_review": bool(target_rec.get("needs_review", False)),
            "host_affinity_applied": False,
            "host_affinity_detail": None,
            "howhard_cell": {"model": qualified_cont, "effort": cont_effort},
            "resolved_target": {
                "agent": cont_agent,
                "model": cont_model,
                "effort": cont_effort,
                "surface": cont_mode,
            },
            "role": str(target_rec.get("role") or "Task Worker"),
            "invocation": None,
            "feedback": None,
            **price_fields,
        }
        if isinstance(target_rec.get("task_text"), str):
            continuation_record["task_text"] = target_rec["task_text"]

    close_record: dict = {
        "format": ROUTE_CLOSE_FORMAT,
        "route_id": resolved_route_id,
        "closed_at": closed_iso,
        "close_status": close_status,
    }
    if obs_id is not None:
        close_record["linked_execution_id"] = obs_id
    if from_id is not None:
        close_record["switched_from"] = from_id
    if sup_id is not None:
        close_record["superseded_by"] = sup_id
    if tokens is not None:
        close_record["tokens"] = tokens
    if sec_val is not None:
        close_record["seconds"] = sec_val
    if cred_val is not None:
        close_record["credit_usd"] = cred_val
    if note_txt is not None:
        close_record["note"] = note_txt

    effective_res_credit = cred_val if cred_val is not None else linked_obs_credit
    with _usage_locked(config):
        claim(config)
        records = [close_record]
        if continuation_record is not None:
            continuation_record["repository_id"] = config.project_identity or config.capture_root.name
            records.append(continuation_record)
        append_route_records(config.state_root, records)
        reconcile_reservation_on_close_locked(
            config,
            resolved_route_id,
            close_status=close_status,
            credit_usd=effective_res_credit,
        )
    register_repository(config)

    result = deepcopy(close_record)
    if continuation_route_id is not None:
        result["continuation_route_id"] = continuation_route_id
    if price_warning is not None:
        result["warnings"] = [f"close: {price_warning}"]
    return result


def load_repository_routes(capture_root: Path, state_root: Path, repository_id: str) -> list[dict]:
    """Load and correlate agr plan records with agr close events, howhard decisions, and outcomes."""
    raw_route_records = read_route_records(state_root)
    plan_records: list[dict] = []
    close_by_route: dict[str, dict] = {}
    for item in raw_route_records:
        if item.get("format") == ROUTE_CLOSE_FORMAT:
            rid = item.get("route_id")
            if isinstance(rid, str) and rid and item.get("close_status") in CLOSE_STATUSES:
                close_by_route[rid] = item
        else:
            plan_records.append(item)

    hh_root = _resolve_howhard_root(capture_root, state_root)

    obs_by_decision: dict[str, dict] = {}
    raw_observations: list[dict] = []
    hh_events: list[dict] = []
    if hh_root is not None:
        obs_by_decision, raw_observations = _load_howhard_observations(hh_root)
        hh_events = list(_read_cached_jsonl(hh_root / "decision-events.jsonl"))
        hh_events.extend(howhard_payload_decisions(hh_root))

    raw_by_id = {str(o.get("id")): o for o in raw_observations if o.get("id")}
    linked_raw_ids: set[str] = set()

    routes: list[dict] = []
    covered_decisions: set[str] = set()

    for rec in plan_records:
        route_id = str(rec.get("route_id") or "")
        if not route_id:
            continue
        dec_id = rec.get("decision_id") if isinstance(rec.get("decision_id"), str) and rec.get("decision_id") else None
        if dec_id:
            covered_decisions.add(dec_id)

        dec_doc = decision_summary(state_root, dec_id, hh_root, _summarize_decision) if (dec_id and hh_root is not None) else None
        close_rec = close_by_route.get(route_id)

        obs = obs_by_decision.get(dec_id) if dec_id else None
        outcome_source: str | None = "matched_receipt" if obs is not None else None
        if obs is None and close_rec is not None:
            linked_id = close_rec.get("linked_execution_id")
            if isinstance(linked_id, str) and linked_id in raw_by_id:
                obs = raw_by_id[linked_id]
                outcome_source = "linked_raw"
                linked_raw_ids.add(linked_id)

        resolved = rec.get("resolved_target") if isinstance(rec.get("resolved_target"), dict) else {}
        host_agent = str(rec.get("host_agent") or "agy")
        target_agent = str(rec.get("target_agent") or resolved.get("agent") or host_agent)
        model = str(resolved.get("model") or "unknown")
        effort = str(resolved.get("effort") or "standard")
        qualified = str((rec.get("howhard_cell") or {}).get("model") or f"{target_agent}/{model}")
        needs_review = bool(rec.get("needs_review", False))
        cap_met = rec.get("cap_met")

        hh_outcome = obs.get("outcome") if isinstance(obs, dict) and obs.get("outcome") in OBSERVED_STATUSES else None
        if close_rec is not None:
            route_status = str(close_rec["close_status"])
        elif hh_outcome:
            route_status = "done"
        elif needs_review or cap_met is False:
            route_status = "review"
        else:
            route_status = "routed"

        if hh_outcome:
            outcome: str | None = str(hh_outcome)
        elif route_status == "done":
            outcome = "unscored"
            outcome_source = "unscored"
        else:
            outcome = None

        if close_rec is not None and route_status in ("switched", "cancelled"):
            status = route_status
        elif hh_outcome:
            status = str(hh_outcome)
        else:
            status = route_status

        task_text = None
        if isinstance(dec_doc, dict):
            ctx = dec_doc.get("context")
            if isinstance(ctx, dict) and isinstance(ctx.get("task_text"), str):
                task_text = ctx["task_text"]
        if not task_text and isinstance(rec.get("task_text"), str):
            task_text = rec["task_text"]
        if not task_text and isinstance(obs, dict) and isinstance(obs.get("task_text"), str):
            task_text = obs["task_text"]
        if not task_text and isinstance(rec.get("invocation"), dict):
            inv_params = rec["invocation"].get("parameters") or {}
            for pk in ("brief", "Prompt", "prompt"):
                if isinstance(inv_params.get(pk), str) and inv_params[pk].strip():
                    task_text = inv_params[pk]
                    break

        role = str(rec.get("role") or "Task Worker")
        label = _first_line(task_text, fallback=role if role != "Task Worker" else (obs.get("id") if obs else role))
        task_type = str(
            (dec_doc or {}).get("task_type")
            or (obs or {}).get("task_type")
            or "task"
        )
        risk_class = (dec_doc or {}).get("risk_class") or (obs or {}).get("risk_class")

        obs_tokens = obs.get("tokens") if isinstance(obs, dict) and isinstance(obs.get("tokens"), int) else None
        close_tokens = close_rec.get("tokens") if isinstance(close_rec, dict) and isinstance(close_rec.get("tokens"), int) else None
        obs_secs = float(obs["seconds"]) if isinstance(obs, dict) and type(obs.get("seconds")) in (int, float) else None
        close_secs = float(close_rec["seconds"]) if isinstance(close_rec, dict) and type(close_rec.get("seconds")) in (int, float) else None
        obs_note = str(obs["note"]) if isinstance(obs, dict) and obs.get("note") else None
        close_note = str(close_rec["note"]) if isinstance(close_rec, dict) and close_rec.get("note") else None
        combined_note = (
            f"{obs_note}\nClose note: {close_note}"
            if (obs_note and close_note and obs_note != close_note)
            else (close_note or obs_note)
        )
        observed_at = (
            _normalize_iso_utc(obs.get("observed_at"))
            if isinstance(obs, dict) and obs.get("observed_at")
            else (_normalize_iso_utc(close_rec.get("closed_at")) if isinstance(close_rec, dict) and close_rec.get("closed_at") else None)
        )
        switched_from_val = (
            (close_rec or {}).get("switched_from")
            or rec.get("switched_from")
        )
        superseded_by_val = (close_rec or {}).get("superseded_by")

        route_entry = {
            "route_id": route_id,
            "decision_id": dec_id,
            "status": status,
            "route_status": route_status,
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
            "observed_at": observed_at,
            "outcome": outcome,
            "outcome_source": outcome_source,
            "cause": str(obs.get("cause")) if isinstance(obs, dict) and obs.get("cause") else None,
            "execution_id": str(obs.get("id")) if isinstance(obs, dict) and obs.get("id") else None,
            "switched_from": str(switched_from_val) if switched_from_val else None,
            "superseded_by": str(superseded_by_val) if superseded_by_val else None,
            "tokens": close_tokens if close_tokens is not None else obs_tokens,
            "seconds": close_secs if close_secs is not None else obs_secs,
            "cost_usd": float(obs["cost_usd"]) if isinstance(obs, dict) and type(obs.get("cost_usd")) in (int, float) else None,
            "invocation": rec.get("invocation") if isinstance(rec.get("invocation"), dict) else None,
            "forecasts": _extract_forecasts(dec_doc),
            "note": combined_note,
            "task_text": task_text[:4000] if isinstance(task_text, str) else None,
        }
        if isinstance(close_rec, dict) and type(close_rec.get("credit_usd")) in (int, float):
            route_entry["credit_usd"] = float(close_rec["credit_usd"])
        if isinstance(rec.get("credit_budget"), dict):
            route_entry["credit_budget"] = deepcopy(rec["credit_budget"])
        if type(rec.get("needed_credit_usd")) in (int, float):
            route_entry["needed_credit_usd"] = float(rec["needed_credit_usd"])
        for key in (
            "credit_p80_usd", "session_cost_usd", "cost_per_credit",
            "monthly_cost_usd", "weekly_cost_usd", "usd_per_mtok",
        ):
            if type(rec.get(key)) in (int, float):
                route_entry[key] = float(rec[key])
        if isinstance(rec.get("rate_card"), dict):
            route_entry["rate_card"] = deepcopy(rec["rate_card"])
        routes.append(route_entry)

    for ev in hh_events:
        dec_id = ev.get("decision_id")
        if not isinstance(dec_id, str) or not dec_id or dec_id in covered_decisions:
            continue
        covered_decisions.add(dec_id)
        dec_doc = decision_summary(state_root, dec_id, hh_root, _summarize_decision) if hh_root is not None else None
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
        needs_review = bool(_decision_selection_flag(dec_doc, "needs_review", False))
        cap_met = _decision_selection_flag(dec_doc, "cap_met")
        if outcome:
            route_status = "done"
            status = str(outcome)
        elif needs_review or cap_met is False:
            route_status = "review"
            status = "review"
        else:
            route_status = "routed"
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

        entry = {
            "route_id": f"hh-{dec_id[:12]}",
            "decision_id": dec_id,
            "status": status,
            "route_status": route_status,
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
            "outcome_source": "matched_receipt" if outcome else None,
            "cause": str(obs.get("cause")) if isinstance(obs, dict) and obs.get("cause") else None,
            "execution_id": str(obs.get("id")) if isinstance(obs, dict) and obs.get("id") else None,
            "switched_from": None,
            "superseded_by": None,
            "tokens": obs.get("tokens") if isinstance(obs, dict) and isinstance(obs.get("tokens"), int) else None,
            "seconds": float(obs["seconds"]) if isinstance(obs, dict) and type(obs.get("seconds")) in (int, float) else None,
            "cost_usd": float(obs["cost_usd"]) if isinstance(obs, dict) and type(obs.get("cost_usd")) in (int, float) else None,
            "invocation": None,
            "forecasts": _extract_forecasts(dec_doc),
            "note": str(obs.get("note")) if isinstance(obs, dict) and obs.get("note") else None,
            "task_text": task_text[:4000] if isinstance(task_text, str) else None,
        }
        chosen_cell = _chosen_selection_cell(dec_doc, chosen)
        if isinstance(chosen_cell, dict):
            if isinstance(chosen_cell.get("credit_budget"), dict):
                entry["credit_budget"] = deepcopy(chosen_cell["credit_budget"])
            for key in ("session_cost_usd", "needed_credit_usd", "credit_p80_usd", "cost_per_credit"):
                if type(chosen_cell.get(key)) in (int, float):
                    entry[key] = float(chosen_cell[key])
        routes.append(entry)

    for obs in raw_observations:
        obs_id = str(obs.get("id") or "")
        if not obs_id or obs_id in linked_raw_ids:
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
                "route_status": None,
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
                "outcome_source": "standalone_raw",
                "cause": str(obs.get("cause")) if obs.get("cause") else None,
                "execution_id": obs_id,
                "switched_from": None,
                "superseded_by": None,
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
    if writable_store(router.config.state_root) and _resolve_howhard_root(
        router.config.capture_root, router.config.state_root
    ) is not None:
        with _usage_locked(router.config):
            claim(router.config)
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
    routed_count = sum(1 for r in all_routes if r.get("route_status") in ("routed", "review"))
    observed_count = sum(1 for r in all_routes if r.get("route_status") not in ("routed", "review"))
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

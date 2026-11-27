"""Core candidate filtering, howhard inventory generation, and execution plan resolution."""

from copy import deepcopy
import json
import re

from .adapters import get_adapter
from .calibration import build_agent_calibration_summary, read_calibration_store
from .config import load_config, resolve_effective_plan_credit, resolve_plan_monthly_cost, set_agent_plan_in_config
from .discovery import discover_agent_candidates
from .price_store import (
    PENDING_FORMAT,
    build_price_request,
    planned_cell_price,
    read_snapshot,
    resolve_prices,
    show_prices,
    snapshot_path,
)
from .pricing import cell_price, needs_price_resolution
from .routes import build_snapshot, close_plan_route, find_route, mint_route_id, record_plan_route, register_repository
from .usage import (
    _now_utc,
    _usage_locked,
    active_reserved_credit_by_pool,
    apply_usage_patch,
    clear_usage,
    effective_agent_state,
    ensure_fresh_usage,
    evaluate_agent_quota,
    evaluate_candidate_tier,
    parse_native_usage,
    probe_usage,
    read_store,
    record_credit_reservation_locked,
    resolve_model_quota,
    routing_freshness_reason,
    usage_warnings,
    visible_agent_state,
)
from .validation import (
    AGENTS,
    AgRouteError,
    agent_id,
    boolean,
    effort_id,
    format_timestamp,
    number,
    table,
    text,
)

PLAN_FORMAT = "agroute-execution-plan-v1"


def parse_qualified_model(qualified, where="model"):
    text(qualified, where, max_length=256)
    for sep in ("/", ":"):
        if sep in qualified:
            prefix, rest = qualified.split(sep, 1)
            agent_id(prefix, f"{where} agent prefix")
            text(rest, f"{where} native model ID", max_length=200)
            if "/" in rest or ":" in rest:
                raise AgRouteError(f"{where}: model ID '{qualified}' contains multiple separators")
            return prefix, rest, sep
    raise AgRouteError(
        f"{where}: expected provider-qualified model ID '<agent>/<model>' or '<agent>:<model>', got '{qualified}'"
    )


def _toml_string(value):
    """A TOML basic string: escape quote, backslash and control characters; keep the rest raw."""
    escaped = "".join(
        "\\" + char if char in '"\\' else f"\\u{ord(char):04x}" if ord(char) < 0x20 or ord(char) == 0x7F else char
        for char in value
    )
    return f'"{escaped}"'


def _toml_comment(value):
    """Text for one TOML comment line: whitespace runs become a space and other control characters are escaped."""
    flat = " ".join(value.split())
    return "".join(f"\\u{ord(char):04x}" if ord(char) < 0x20 or ord(char) == 0x7F else char for char in flat)


def _toml_value(value):
    """Render a candidate cell value as an inline TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        items = (f"{k if re.fullmatch(r'[A-Za-z0-9_-]+', k) else _toml_string(k)} = {_toml_value(v)}"
                 for k, v in value.items())
        return "{ " + ", ".join(items) + " }"
    raise AgRouteError(f"inventory: cannot render {type(value).__name__} as TOML")


def _extract_cell_score(entry):
    for key in ("J", "expected_cost", "sampled_cost", "cost"):
        val = entry.get(key)
        if type(val) in (int, float):
            return float(val)
    return None


def _extract_needed_credit(entry, *, allow_legacy_cost=False):
    if not isinstance(entry, dict):
        return None
    keys = ("needed_credit_usd", "C_run") if allow_legacy_cost else ("needed_credit_usd",)
    for key in keys:
        val = entry.get(key)
        if type(val) in (int, float):
            return float(val)
    return None


def _extract_session_cost(entry, *, allow_legacy_cost=False):
    if not isinstance(entry, dict):
        return None
    keys = ("session_cost_usd", "C_run") if allow_legacy_cost else ("session_cost_usd",)
    for key in keys:
        val = entry.get(key)
        if type(val) in (int, float):
            return float(val)
    return None


_CREDIT_COST_SPLIT_FORMATS = frozenset(
    {"howhard-decision-v7", "howhard-decision-v8", "howhard-decision-v9", "howhard-decision-v10"}
)


def _decision_panels(decision, *, order=("cells", "candidates", "ranking")):
    if not isinstance(decision, dict):
        return []
    panels = []
    for panel_key in order:
        panel = decision.get(panel_key)
        if isinstance(panel, list):
            panels.append(panel)
    selection = decision.get("selection")
    if isinstance(selection, dict) and isinstance(selection.get("cells"), list):
        panels.append(selection["cells"])
    return panels


def _decision_selection_flag(decision, key, default=None):
    if not isinstance(decision, dict):
        return default
    if decision.get(key) is not None:
        return decision[key]
    selection = decision.get("selection")
    if isinstance(selection, dict) and selection.get(key) is not None:
        return selection[key]
    return default


def _decision_has_credit_cost_split(decision):
    if decision.get("format") in _CREDIT_COST_SPLIT_FORMATS:
        return True
    chosen = decision.get("chosen")
    if isinstance(chosen, dict) and (
        "needed_credit_usd" in chosen or "session_cost_usd" in chosen or "cost_per_credit" in chosen
    ):
        return True
    for panel in _decision_panels(decision):
        if any(
            isinstance(cell, dict)
            and ("needed_credit_usd" in cell or "session_cost_usd" in cell or "cost_per_credit" in cell)
            for cell in panel
        ):
            return True
    return False


def _find_panel_cell(decision, cell_key):
    for panel in _decision_panels(decision):
        for item in panel:
            if not isinstance(item, dict):
                continue
            m_raw = item.get("model")
            eff_raw = item.get("effort")
            if not isinstance(m_raw, str) or not isinstance(eff_raw, str):
                continue
            try:
                ag, nat_m, _ = parse_qualified_model(m_raw, "decision panel.model")
            except AgRouteError:
                continue
            if (ag, nat_m, eff_raw) == cell_key:
                return item
    return None


class Router:
    """Plan-only local agent router integrating native quota state with howhard."""

    def __init__(
        self,
        config_path=None,
        *,
        project=None,
        state_directory=None,
        include_user_config=True,
    ):
        self.config = load_config(
            config_path,
            project=project,
            state_directory=state_directory,
            include_user_config=include_user_config,
        )

    def paths(self):
        return {
            "capture_root": str(self.config.capture_root),
            "state_root": str(self.config.state_root),
            "identity_path": str(self.config.identity_path),
            "project_identity": self.config.project_identity or None,
            "config_path": str(self.config.config_path) if self.config.config_path else None,
            "user_config_path": str(self.config.user_config_path) if self.config.user_config_path else None,
        }

    @staticmethod
    def _exhausted_pool_reason(pool_meta: dict) -> str:
        s5 = pool_meta.get("session_5h") or {}
        rem_5h = s5.get("remaining_credit_usd")
        exhausted_win = "session_5h" if (rem_5h is not None and rem_5h <= 0.0) else "weekly_7d"
        win_meta = pool_meta.get(exhausted_win) or {}
        held_back = float(win_meta.get("reserve_credit_usd") or 0.0)
        reserved_amt = float(win_meta.get("reserved_credit_usd") or 0.0)
        parts = []
        if held_back > 0.0:
            parts.append(f"{held_back:.6g} USD reserve")
        if reserved_amt > 0.0:
            parts.append(f"{reserved_amt:.6g} USD reserved")
        after_detail = f" after {' and '.join(parts)}" if parts else ""
        return f"{exhausted_win} credit_budget exhausted (0 USD remaining{after_detail})"

    def _resolve_agent_surface_quotas(self, name, cfg, eff, *, calibration_store=None, reserved_credit_by_pool=None):
        nat_cands = deepcopy(cfg["native_candidates"]) if cfg["native_candidates"] else discover_agent_candidates(name, "native")
        cli_cands = deepcopy(cfg["cli_candidates"]) if cfg["cli_candidates"] else discover_agent_candidates(name, "cli")
        models_map = {}
        cli_quotas = []
        nat_quotas = []
        for surface, cands, is_host, bucket in (
            ("cli", cli_cands, False, cli_quotas),
            ("native", nat_cands, True, nat_quotas),
        ):
            for cand in cands:
                mid = cand["model"]
                q = resolve_model_quota(
                    name,
                    cfg,
                    eff,
                    cand,
                    is_host=is_host,
                    surface=surface,
                    calibration_store=calibration_store,
                    reserved_credit_by_pool=reserved_credit_by_pool,
                )
                bucket.append(q)
                if mid not in models_map:
                    entry = {
                        "surface": surface,
                        "efforts": [cand["effort"]],
                        "quota_group": q["quota_group"],
                        "session_5h": q["session_5h"],
                        "weekly_7d": q["weekly_7d"],
                        "eligible": q["eligible"],
                        "exclusion_reason": q["exclusion_reason"],
                    }
                    if surface == "native":
                        entry["native_eligible"] = q["eligible"]
                        entry["native_exclusion_reason"] = q["exclusion_reason"]
                    if "credit_budget" in q:
                        entry["credit_budget"] = deepcopy(q["credit_budget"])
                    if "credit_calibration" in q:
                        entry["credit_calibration"] = deepcopy(q["credit_calibration"])
                    for key in ("monthly_cost_usd", "weekly_cost_usd", "cost_per_credit"):
                        if key in q:
                            entry[key] = q[key]
                    models_map[mid] = entry
                else:
                    if cand["effort"] not in models_map[mid]["efforts"]:
                        models_map[mid]["efforts"].append(cand["effort"])
                    if surface == "native" and "native_eligible" not in models_map[mid]:
                        models_map[mid]["native_eligible"] = q["eligible"]
                        models_map[mid]["native_exclusion_reason"] = q["exclusion_reason"]
        return models_map, cli_quotas, nat_quotas

    def _agent_model_quotas(self, name, cfg, eff, *, calibration_store=None, reserved_credit_by_pool=None):
        models_map, _, _ = self._resolve_agent_surface_quotas(
            name,
            cfg,
            eff,
            calibration_store=calibration_store,
            reserved_credit_by_pool=reserved_credit_by_pool,
        )
        return models_map

    def usage_show(self, agent=None, *, now=None):
        now_dt = _now_utc(now)
        store = read_store(self.config)
        cal_store = read_calibration_store(self.config)
        names = [agent_id(agent, "agent")] if agent is not None else list(AGENTS)
        agents_out = {}
        warnings = []
        for name in names:
            cfg = self.config.agents[name]
            warnings.extend(usage_warnings(name, store["agents"][name], cfg, now_dt))
            eff = visible_agent_state(name, store["agents"][name], cfg, now_dt)
            reserved_by_pool = active_reserved_credit_by_pool(self.config, name, eff, now_dt)
            effective_plan, _ = resolve_effective_plan_credit(
                name, cfg, eff.get("detected_plan")
            )
            monthly_cost = resolve_plan_monthly_cost(name, cfg, eff.get("detected_plan"))
            ok_target, reason_target = evaluate_agent_quota(name, cfg, eff, is_host=False)
            ok_host, reason_host = evaluate_agent_quota(name, cfg, eff, is_host=True)
            cal_target = build_agent_calibration_summary(
                name,
                cfg,
                eff,
                is_host=False,
                calibration_store=cal_store,
                reserved_credit_by_pool=reserved_by_pool,
            )
            cal_host = build_agent_calibration_summary(
                name,
                cfg,
                eff,
                is_host=True,
                calibration_store=cal_store,
                reserved_credit_by_pool=reserved_by_pool,
            )
            models_map, cli_quotas, nat_quotas = self._resolve_agent_surface_quotas(
                name,
                cfg,
                eff,
                calibration_store=cal_store,
                reserved_credit_by_pool=reserved_by_pool,
            )
            excluded_groups = set(cfg.get("exclude_quota_groups", ()))
            if ok_target:
                if cli_quotas:
                    if not any(q["eligible"] for q in cli_quotas):
                        ok_target = False
                        reason_target = cli_quotas[0]["exclusion_reason"]
                else:
                    known_target = [
                        p
                        for grp, p in cal_target.items()
                        if grp not in excluded_groups and p.get("available_credit_usd") is not None
                    ]
                    if known_target and all(p["available_credit_usd"] <= 0.0 for p in known_target):
                        ok_target = False
                        reason_target = self._exhausted_pool_reason(known_target[0])
            if ok_host:
                if nat_quotas:
                    if not any(q["eligible"] for q in nat_quotas):
                        ok_host = False
                        reason_host = nat_quotas[0]["exclusion_reason"]
                else:
                    known_host = [
                        p
                        for grp, p in cal_host.items()
                        if grp not in excluded_groups and p.get("available_credit_usd") is not None
                    ]
                    if known_host and all(p["available_credit_usd"] <= 0.0 for p in known_host):
                        ok_host = False
                        reason_host = self._exhausted_pool_reason(known_host[0])
            agents_out[name] = {
                **eff,
                "enabled": cfg["enabled"],
                "plan": cfg["plan"],
                "detected_plan": eff.get("detected_plan"),
                "effective_plan": effective_plan,
                "monthly_cost_usd": monthly_cost,
                "weekly_cost_usd": monthly_cost * 7.0 / 30.0 if monthly_cost is not None else None,
                "quota_limits": {
                    "max_session_used_pct": cfg["max_session_used_pct"],
                    "max_weekly_used_pct": cfg["max_weekly_used_pct"],
                    "max_tier_used_pct": cfg["max_tier_used_pct"],
                    "host_reserve_pct": cfg["host_reserve_pct"],
                    "min_remaining_credit_usd": cfg["min_remaining_credit_usd"],
                    "host_reserve_credit_usd": cfg["host_reserve_credit_usd"],
                    "exclude_quota_groups": list(cfg.get("exclude_quota_groups", ())),
                },
                "credit_calibration": cal_target,
                "host_credit_calibration": cal_host,
                "eligible_as_target": ok_target,
                "target_exclusion_reason": reason_target,
                "eligible_as_host_native": ok_host,
                "host_native_exclusion_reason": reason_host,
                "models": models_map,
            }
        return {
            "format": "agroute-usage-snapshot-v1",
            "evaluated_at": format_timestamp(now_dt),
            "agents": agents_out,
            "warnings": warnings,
        }

    def usage_set(
        self,
        agent,
        *,
        session_used_pct=None,
        session_reset_at=None,
        weekly_used_pct=None,
        weekly_reset_at=None,
        rate_limited=None,
        cooldown_until=None,
        tier=None,
        tier_used_pct=None,
        tier_reset_at=None,
        now=None,
    ):
        agent_id(agent, "agent")
        patch = {}
        if session_used_pct is not None or session_reset_at is not None:
            patch["session_5h"] = {"used_pct": session_used_pct, "reset_at": session_reset_at}
        if weekly_used_pct is not None or weekly_reset_at is not None:
            patch["weekly_7d"] = {"used_pct": weekly_used_pct, "reset_at": weekly_reset_at}
        if tier is not None or tier_used_pct is not None or tier_reset_at is not None:
            if tier is None:
                raise AgRouteError("usage set: --tier is required when setting tier usage or reset")
            text(tier, "tier", label=True, max_length=64)
            if rate_limited is not None:
                if tier_reset_at is not None and cooldown_until is not None and tier_reset_at != cooldown_until:
                    raise AgRouteError(
                        "usage set: specify either --tier-reset-at or --cooldown-until for a tier, not conflicting values"
                    )
                effective_tier_reset = tier_reset_at if tier_reset_at is not None else cooldown_until
                is_rl = boolean(rate_limited, "rate_limited")
                patch["tiers"] = {
                    tier: {
                        "used_pct": tier_used_pct,
                        "reset_at": effective_tier_reset,
                        "status": "rejected" if is_rl else "allowed",
                    }
                }
            else:
                patch["tiers"] = {tier: {"used_pct": tier_used_pct, "reset_at": tier_reset_at}}
                if cooldown_until is not None:
                    patch["cooldown_until"] = cooldown_until
        else:
            if rate_limited is not None:
                patch["rate_limited"] = boolean(rate_limited, "rate_limited")
            if cooldown_until is not None:
                patch["cooldown_until"] = cooldown_until
        if not patch:
            raise AgRouteError("usage set: at least one quota or cooldown field must be specified")
        updated = apply_usage_patch(self.config, agent, patch, source="set", now=now)
        return {"agent": agent, "usage": updated}

    def usage_ingest(self, agent, raw_text, *, tier=None, now=None):
        agent_id(agent, "agent")
        patch = parse_native_usage(agent, raw_text, tier=tier, now=format_timestamp(_now_utc(now)))
        updated = apply_usage_patch(self.config, agent, patch, source="ingest", now=now)
        return {"agent": agent, "usage": updated}

    def usage_probe(self, agent=None, *, now=None):
        probed = probe_usage(self.config, agent, now=now)
        return {"probed": probed, "warnings": [warning for state in probed.values() for warning in state.get("probe", {}).get("warnings", [])]}

    def tui_probe(self, manual=False, *, now=None):
        """Probe enabled providers, restricting automatic attempts to stale data."""
        from .usage import _freshness_reason, _now_utc, read_store

        store = read_store(self.config)
        for name, cfg in self.config.agents.items():
            if not cfg["enabled"]:
                continue
            if not manual and _freshness_reason(store["agents"][name], cfg, _now_utc(now)) is None:
                continue
            try:
                self.usage_probe(name, now=now)
            except (AgRouteError, OSError):
                continue

    def usage_clear(self, agent=None):
        cleared = clear_usage(self.config, agent)
        return {"cleared": cleared}

    def _collect_eligible(self, host, *, include_agents=None, now=None, priced=True):
        host = agent_id(host or self.config.default_host, "host")
        now_dt = _now_utc(now)
        sep = self.config.namespace_separator

        if include_agents is not None:
            if not isinstance(include_agents, (list, tuple)) or not include_agents:
                raise AgRouteError("include_agents: expected a non-empty list of agent IDs")
            agent_order = [agent_id(a, "include_agents") for a in include_agents]
        else:
            # Put host agent first so native candidates appear first in deterministic order
            agent_order = [host] + [a for a in AGENTS if a != host]

        store, refresh_failures = ensure_fresh_usage(self.config, agent_order, host=host, now=now)
        now_dt = _now_utc(now)
        cal_store = read_calibration_store(self.config)

        admitted = []
        admitted_meta = {}
        excluded = []
        warnings = []
        pending_prices: dict[str, dict] = {}
        credit_calibration_out: dict[str, dict] = {}
        # Execution planning needs eligibility only, so it never depends on price state.
        snapshot = read_snapshot(self.config) if priced else None

        for name in agent_order:
            cfg = self.config.agents[name]
            adapter = get_adapter(name)
            warnings.extend(usage_warnings(name, store["agents"][name], cfg, now_dt))
            eff = visible_agent_state(name, store["agents"][name], cfg, now_dt)
            reserved_by_pool = active_reserved_credit_by_pool(self.config, name, eff, now_dt)
            is_host = name == host
            cal_summary = build_agent_calibration_summary(
                name,
                cfg,
                eff,
                is_host=is_host,
                calibration_store=cal_store,
                reserved_credit_by_pool=reserved_by_pool,
            )
            if cal_summary:
                credit_calibration_out[name] = cal_summary
            ok_agent, reason_agent = evaluate_agent_quota(name, cfg, eff, is_host=is_host)
            freshness = routing_freshness_reason(store["agents"][name], cfg, now_dt, is_host=is_host)
            if cfg["enabled"] and freshness and name not in refresh_failures:
                refresh_failures[name] = (f"{name} quota refresh infrastructure incident at admission: {freshness}; "
                                          f"retry with agr usage probe --agent {name}")
            if name in refresh_failures:
                ok_agent, reason_agent = False, refresh_failures[name]
                warnings.append(reason_agent)
            if not ok_agent:
                excluded.append(
                    {
                        "agent": name,
                        "surface": "native" if is_host else "cli",
                        "model": None,
                        "effort": None,
                        "reason": reason_agent,
                    }
                )
                continue

            surface_name = "native" if is_host else "cli"
            configured_list = cfg["native_candidates"] if is_host else cfg["cli_candidates"]
            is_discovered = not configured_list
            source_list = configured_list if configured_list else discover_agent_candidates(name, surface_name)
            if not source_list:
                excluded.append(
                    {
                        "agent": name,
                        "surface": surface_name,
                        "model": None,
                        "effort": None,
                        "reason": f"no {surface_name}_candidates discovered or configured for '{name}'",
                    }
                )
                continue

            if priced:
                for message in self._unmatched_price_warnings(name, cfg, surface_name, source_list):
                    if message not in warnings:
                        warnings.append(message)

            for cand in source_list:
                mq = resolve_model_quota(
                    name,
                    cfg,
                    eff,
                    cand,
                    is_host=is_host,
                    surface=surface_name,
                    calibration_store=cal_store,
                    reserved_credit_by_pool=reserved_by_pool,
                )
                if not mq["eligible"]:
                    excluded.append(
                        {
                            "agent": name,
                            "surface": surface_name,
                            "model": cand["model"],
                            "effort": cand["effort"],
                            "reason": mq["exclusion_reason"],
                        }
                    )
                    continue
                qualified = f"{name}{sep}{cand['model']}"
                cell = {"model": qualified, "effort": cand["effort"]}
                cell["cost_per_credit"] = mq.get("cost_per_credit", 1.0)
                if priced:
                    if is_discovered:
                        self._record_pending_price(
                            name,
                            cfg,
                            adapter,
                            cand,
                            surface_name,
                            qualified,
                            now_dt,
                            snapshot,
                            pending_prices,
                        )
                    catalog_key = adapter.rate_card_key(cand["model"], cand["effort"], surface_name)
                    cell.update(cell_price(name, cand, cfg["prices"], catalog_key, qualified, now_dt, snapshot))
                    if mq.get("credit_budget") is not None:
                        if "rate_card" not in cell and "usd_per_mtok" not in cell:
                            excluded.append(
                                {
                                    "agent": name,
                                    "surface": surface_name,
                                    "model": cand["model"],
                                    "effort": cand["effort"],
                                    "reason": "model has no known price required for credit_budget evaluation",
                                }
                            )
                            continue
                        cell["credit_budget"] = deepcopy(mq["credit_budget"])
                admitted.append(cell)
                admitted_meta[(name, cand["model"], cand["effort"])] = {
                    "qualified_model": qualified,
                    "surface": surface_name,
                    "candidate": deepcopy(cand),
                    "credit_budget": deepcopy(mq.get("credit_budget")),
                    # The reserve held back in each budget window, for explaining refusals.
                    "window_reserves": {
                        window: mq["credit_calibration"][window]["reserve_credit_usd"]
                        for window in ("session_5h", "weekly_7d")
                        if f"{window}_remaining_credit_usd" in (mq.get("credit_budget") or {})
                    },
                    "window_reserved": {
                        window: mq["credit_calibration"][window].get("reserved_credit_usd", 0.0)
                        for window in ("session_5h", "weekly_7d")
                        if f"{window}_remaining_credit_usd" in (mq.get("credit_budget") or {})
                    },
                    "reservation_pools": deepcopy(mq.get("reservation_pools", [])),
                    "monthly_cost_usd": mq.get("monthly_cost_usd"),
                    "weekly_cost_usd": mq.get("weekly_cost_usd"),
                    "cost_per_credit": mq.get("cost_per_credit", 1.0),
                }
                # Admitted routes whose windows cannot keep the credit reserve stay visible.
                for message in mq["credit_reserve_warnings"]:
                    if message not in warnings:
                        warnings.append(message)

        price_request = build_price_request(self.config, now_dt, pending_prices) if priced else None
        if not admitted:
            reasons = "; ".join(f"{e['agent']}: {e['reason']}" for e in excluded) or "none configured"
            error = AgRouteError(f"candidates: no eligible candidate routes available ({reasons})")
            error.details = {
                "format": "agroute-candidates-v1",
                "host_agent": host,
                "evaluated_at": format_timestamp(now_dt),
                "candidates": [],
                "price_request": price_request,
                "credit_calibration": credit_calibration_out,
                "excluded": excluded,
                "warnings": warnings,
            }
            raise error
        return (
            host,
            now_dt,
            admitted,
            admitted_meta,
            excluded,
            warnings,
            price_request,
            credit_calibration_out,
        )

    @staticmethod
    def _unmatched_price_warnings(name, cfg, surface_name, source_list):
        prices = cfg.get("prices") or {}
        if not prices:
            return []
        known_models = {cand["model"] for cand in source_list}
        unmatched = [model for model in prices if model not in known_models]
        if unmatched:
            other_surface = "cli" if surface_name == "native" else "native"
            other_configured = cfg[f"{other_surface}_candidates"]
            other_source = (
                other_configured
                if other_configured
                else discover_agent_candidates(name, other_surface)
            )
            known_models.update(cand["model"] for cand in other_source)
            unmatched = [model for model in unmatched if model not in known_models]
        return [
            f'agents.{name}.prices."{model}": matches no configured or discovered {name} candidate'
            for model in sorted(unmatched)
        ]

    @staticmethod
    def _record_pending_price(
        name,
        cfg,
        adapter,
        cand,
        surface,
        qualified,
        now_dt,
        snapshot,
        pending_prices,
    ):
        if "usd_per_mtok" in cand or "rate_card" in cand or cand["model"] in cfg["prices"]:
            return
        rc_key = adapter.rate_card_key(cand["model"], cand["effort"], surface)
        if not needs_price_resolution(rc_key, now_dt, snapshot):
            return
        slot = pending_prices.setdefault(
            rc_key,
            {"agents": set(), "sample_models": set(), "display_names": [], "expired_at": None},
        )
        slot["agents"].add(name)
        slot["sample_models"].add(qualified)
        disp = cand.get("display_name")
        if isinstance(disp, str) and disp.strip():
            slot["display_names"].append(disp.strip())
        if snapshot is not None and rc_key in snapshot["models"] and now_dt >= snapshot["models"][rc_key]["expires"]:
            slot["expired_at"] = snapshot["models"][rc_key]["expires_at"]

    def prices_pending(self, host=None, *, include_agents=None, now=None):
        """Inspect unpriced or expired discovered models and emit a calling-agent `price_request`."""
        host = agent_id(host or self.config.default_host, "host")
        now_dt = _now_utc(now)
        sep = self.config.namespace_separator
        if include_agents is not None:
            if not isinstance(include_agents, (list, tuple)) or not include_agents:
                raise AgRouteError("include_agents: expected a non-empty list of agent IDs")
            agent_order = [agent_id(a, "include_agents") for a in include_agents]
        else:
            agent_order = [host] + [a for a in AGENTS if a != host]

        snapshot = read_snapshot(self.config)
        pending_prices: dict[str, dict] = {}
        for name in agent_order:
            cfg = self.config.agents[name]
            if not cfg["enabled"]:
                continue
            is_host = name == host
            surface_name = "native" if is_host else "cli"
            configured_list = cfg["native_candidates"] if is_host else cfg["cli_candidates"]
            if configured_list:
                continue
            adapter = get_adapter(name)
            for cand in discover_agent_candidates(name, surface_name):
                qualified = f"{name}{sep}{cand['model']}"
                self._record_pending_price(
                    name,
                    cfg,
                    adapter,
                    cand,
                    surface_name,
                    qualified,
                    now_dt,
                    snapshot,
                    pending_prices,
                )
        req = build_price_request(self.config, now_dt, pending_prices)
        return {
            "format": PENDING_FORMAT,
            "evaluated_at": format_timestamp(now_dt),
            "host_agent": host,
            "state_path": str(snapshot_path(self.config)),
            "pending_count": len(req["unpriced_models"]) if req else 0,
            "price_request": req,
        }

    def prices_resolve(self, payload, *, now=None):
        """Validate and store calling-agent model price resolutions in `<state_root>/prices.json`."""
        return resolve_prices(self.config, payload, _now_utc(now))

    def prices_show(self, *, now=None):
        """Show stored calling-agent price resolutions in `<state_root>/prices.json`."""
        return show_prices(self.config, _now_utc(now))

    def candidates(self, host=None, *, include_agents=None, now=None):
        (
            host,
            now_dt,
            admitted,
            _,
            excluded,
            warnings,
            price_request,
            credit_calibration,
        ) = self._collect_eligible(host, include_agents=include_agents, now=now)
        return {
            "format": "agroute-candidates-v1",
            "host_agent": host,
            "evaluated_at": format_timestamp(now_dt),
            "candidates": admitted,
            "price_request": price_request,
            "credit_calibration": credit_calibration,
            "excluded": excluded,
            "warnings": warnings,
        }

    def inventory(self, host=None, *, name="router", include_agents=None, now=None):
        text(name, "inventory name", label=True, max_length=64)
        res = self.candidates(host, include_agents=include_agents, now=now)
        return {
            "host_agent": res["host_agent"],
            "inventory_name": name,
            "inventories": {name: res["candidates"]},
            "price_request": res["price_request"],
            "credit_calibration": res["credit_calibration"],
            "excluded": res["excluded"],
            "warnings": res["warnings"],
        }

    def format_inventory_toml(self, host=None, *, name="router", include_agents=None, now=None):
        inv = self.inventory(host, name=name, include_agents=include_agents, now=now)
        cells = inv["inventories"][name]
        # Pasted into howhard.toml, each routing warning stays visible as one comment line.
        lines = [f"# warning: {_toml_comment(warning)}" for warning in inv["warnings"]]
        for ag, groups in (inv.get("credit_calibration") or {}).items():
            for grp, meta in groups.items():
                parts = []
                for win in ("session_5h", "weekly_7d"):
                    if win in meta:
                        w = meta[win]
                        parts.append(
                            f"{win}={w['calibrated_credit_usd']:.6g} USD credit "
                            f"(prior={w['prior_credit_usd']}, n={w['matched_intervals']}, "
                            f"windows={w['effective_sample_windows']:.3g})"
                        )
                        if w.get("weekly_cost_usd") is not None:
                            parts.append(
                                f"weekly_cost_usd={w['weekly_cost_usd']:.6g}, "
                                f"cost_per_credit={w['cost_per_credit']:.6g}"
                            )
                summary = []
                if meta.get("monthly_cost_usd") is not None:
                    summary.extend(
                        [
                            f"monthly_cost_usd={meta['monthly_cost_usd']:.6g}",
                            f"weekly_cost_usd={meta['weekly_cost_usd']:.6g}",
                        ]
                    )
                if meta.get("available_credit_usd") is not None:
                    summary.append(f"available_credit_usd={meta['available_credit_usd']:.6g}")
                lines.append(f"# credit_calibration {ag}.{grp}: {', '.join(summary + parts)}")
        lines.extend(["[inventories]", f"{name} = ["])
        for cell in cells:
            lines.append(f"  {_toml_value(cell)},")
        lines.append("]")
        return "\n".join(lines) + "\n"

    def plan(
        self,
        host=None,
        decision=None,
        *,
        prompt=None,
        role="Task Worker",
        subagent_type=None,
        workspace="inherit",
        include_agents=None,
        now=None,
    ):
        host, now_dt, _, admitted_meta, excluded, warnings, _, _ = self._collect_eligible(
            host, include_agents=include_agents, now=now, priced=False
        )
        table(decision, "decision")
        decision_id = text(decision.get("decision_id"), "decision.decision_id", max_length=128)
        chosen = table(decision.get("chosen"), "decision.chosen")
        chosen_raw_model = text(chosen.get("model"), "decision.chosen.model", max_length=256)
        chosen_effort = effort_id(chosen.get("effort"), "decision.chosen.effort")
        chosen_agent, chosen_native_model, _ = parse_qualified_model(
            chosen_raw_model, "decision.chosen.model"
        )

        if prompt is not None:
            text(prompt, "prompt", max_length=1_000_000)
        text(role, "role", max_length=128)
        text(workspace, "workspace", label=True, max_length=32)
        if subagent_type is not None:
            text(subagent_type, "subagent_type", max_length=64)

        chosen_key = (chosen_agent, chosen_native_model, chosen_effort)
        if chosen_key not in admitted_meta:
            ex_reasons = [
                e["reason"]
                for e in excluded
                if e["agent"] == chosen_agent and (e["model"] in (None, chosen_native_model))
            ]
            detail = f" ({'; '.join(ex_reasons)})" if ex_reasons else ""
            raise AgRouteError(
                f"plan: chosen cell ({chosen_raw_model!r}, {chosen_effort!r}) is not an eligible route for host '{host}'{detail}"
            )

        target_agent = chosen_agent
        target_model = chosen_native_model
        target_effort = chosen_effort
        host_affinity_applied = False
        affinity_detail = None

        chosen_panel_item = _find_panel_cell(decision, chosen_key)
        target_panel_item = chosen_panel_item
        allow_legacy_cost = not _decision_has_credit_cost_split(decision)
        needed_credit = _extract_needed_credit(chosen_panel_item, allow_legacy_cost=allow_legacy_cost)
        if needed_credit is None:
            needed_credit = _extract_needed_credit(chosen, allow_legacy_cost=allow_legacy_cost)
        session_cost = _extract_session_cost(chosen_panel_item, allow_legacy_cost=allow_legacy_cost)
        if session_cost is None:
            session_cost = _extract_session_cost(chosen, allow_legacy_cost=allow_legacy_cost)

        epsilon = self.config.host_affinity_epsilon
        if epsilon > 0.0 and chosen_agent != host:
            panels = _decision_panels(decision, order=("candidates", "cells", "ranking"))
            panel = panels[0] if panels else None
            if panel:
                chosen_score = None
                best_native = None
                for item in panel:
                    if not isinstance(item, dict):
                        continue
                    m_raw = item.get("model")
                    eff_raw = item.get("effort")
                    if not isinstance(m_raw, str) or not isinstance(eff_raw, str):
                        continue
                    try:
                        ag, nat_m, _ = parse_qualified_model(m_raw, "decision panel.model")
                    except AgRouteError:
                        continue
                    score = _extract_cell_score(item)
                    if score is None:
                        continue
                    if (ag, nat_m, eff_raw) == chosen_key:
                        chosen_score = score
                    elif (
                        ag == host
                        and (ag, nat_m, eff_raw) in admitted_meta
                        and item.get("cap_met") is not False
                        and item.get("admissible") is not False
                    ):
                        nat_meta = admitted_meta[(ag, nat_m, eff_raw)]
                        nat_budget = nat_meta.get("credit_budget")
                        nat_needed = _extract_needed_credit(item, allow_legacy_cost=allow_legacy_cost)
                        if (
                            nat_budget is not None
                            and nat_needed is not None
                            and nat_needed > nat_budget["available_credit_usd"]
                        ):
                            continue
                        if best_native is None or score < best_native[0]:
                            best_native = (score, ag, nat_m, eff_raw, nat_needed, item)
                if chosen_score is not None and best_native is not None:
                    native_score, nat_ag, nat_m, nat_eff, nat_needed, nat_item = best_native
                    if native_score <= chosen_score + epsilon:
                        target_agent = nat_ag
                        target_model = nat_m
                        target_effort = nat_eff
                        needed_credit = nat_needed
                        target_panel_item = nat_item
                        session_cost = _extract_session_cost(nat_item, allow_legacy_cost=allow_legacy_cost)
                        host_affinity_applied = True
                        affinity_detail = {
                            "chosen_score": chosen_score,
                            "native_score": native_score,
                            "delta": native_score - chosen_score,
                            "epsilon": epsilon,
                        }

        meta = admitted_meta[(target_agent, target_model, target_effort)]
        qualified_target_model = meta["qualified_model"]
        live_budget = meta.get("credit_budget")
        window_reserves = dict(meta["window_reserves"])
        window_reserved = dict(meta.get("window_reserved", {}))
        reservation_pools = deepcopy(meta.get("reservation_pools", []))

        def _check_credit_fit(budget, reserves, reserved):
            if budget is not None and needed_credit is not None and needed_credit > budget["available_credit_usd"]:
                window_parts = []
                if "session_5h_remaining_credit_usd" in budget:
                    window_parts.append(
                        f"session_5h_remaining_credit_usd={budget['session_5h_remaining_credit_usd']:.6g}"
                    )
                if "weekly_7d_remaining_credit_usd" in budget:
                    window_parts.append(
                        f"weekly_7d_remaining_credit_usd={budget['weekly_7d_remaining_credit_usd']:.6g}"
                    )
                for window, held_back in reserves.items():
                    if held_back > 0.0:
                        window_parts.append(f"{window}_reserve_credit_usd={held_back:.6g}")
                for window, in_flight in reserved.items():
                    if in_flight > 0.0:
                        window_parts.append(f"{window}_reserved_credit_usd={in_flight:.6g}")
                window_detail = f" ({', '.join(window_parts)})" if window_parts else ""
                raise AgRouteError(
                    f"plan: chosen cell ({qualified_target_model!r}, {target_effort!r}) requires {needed_credit:.6g} USD credit, "
                    f"exceeding remaining credit {budget['available_credit_usd']:.6g} USD{window_detail}"
                )

        _check_credit_fit(live_budget, window_reserves, window_reserved)
        # Freeze the target cell's price exactly as `candidates` emits it now, so later usage
        # accounting never needs a retroactive lookup. Price state never affects eligibility.
        price_fields, price_warning = planned_cell_price(
            self.config, target_agent, meta["candidate"], meta["surface"], qualified_target_model, now_dt
        )
        if price_warning is not None:
            warnings.append(f"plan: {price_warning}")
        cell_cost_per_credit = meta.get("cost_per_credit", 1.0)
        if isinstance(target_panel_item, dict) and type(target_panel_item.get("cost_per_credit")) in (int, float):
            cell_cost_per_credit = float(target_panel_item["cost_per_credit"])
        if session_cost is None and needed_credit is not None:
            session_cost = needed_credit * cell_cost_per_credit
        is_native = target_agent == host
        execution_mode = "native" if is_native else "cli"

        if is_native:
            adapter = get_adapter(host)
            invocation = adapter.build_native_invocation(
                target_model,
                target_effort,
                prompt=prompt,
                role=role,
                subagent_type=subagent_type,
                workspace=workspace,
            )
            cli_res = adapter.build_cli_invocation(
                target_model,
                target_effort,
                prompt=prompt,
            )
            fallback = {
                "kind": "cli_command",
                "argv": cli_res["argv"],
                "prompt_delivery": cli_res["prompt_delivery"],
                "output_capture_adapter": cli_res["capture_adapter"],
            }
        else:
            adapter = get_adapter(target_agent)
            cli_res = adapter.build_cli_invocation(
                target_model,
                target_effort,
                prompt=prompt,
            )
            invocation = {
                "kind": "cli_command",
                "argv": cli_res["argv"],
                "prompt_delivery": cli_res["prompt_delivery"],
                "output_capture_adapter": cli_res["capture_adapter"],
            }
            fallback = None

        evaluated_iso = format_timestamp(now_dt)
        planned_route_id = mint_route_id(
            evaluated_iso,
            decision_id,
            host,
            target_agent,
            target_model,
            target_effort,
        )

        if not host_affinity_applied:
            feedback = {
                "mode": "matched_receipt",
                "decision_id": decision_id,
                "model": qualified_target_model,
                "effort": target_effort,
                "command_template": [
                    "howhard",
                    "observe",
                    decision_id,
                    "<clean|loud|silent|interrupted>",
                    "--id",
                    "<EXECUTION_ID>",
                    "--json",
                ],
            }
        else:
            feedback = {
                "mode": "raw_native",
                "decision_id": None,
                "unexecuted_decision_id": decision_id,
                "model": qualified_target_model,
                "effort": target_effort,
                "note": (
                    f"Host-affinity override (epsilon={epsilon}) executed native cell "
                    f"{qualified_target_model} ({target_effort}) instead of {chosen_raw_model} ({chosen_effort})"
                ),
                "command_template": [
                    "howhard",
                    "observe",
                    "<TASK>",
                    qualified_target_model,
                    target_effort,
                    "<clean|loud|silent|interrupted>",
                    "--native",
                    "--id",
                    "<EXECUTION_ID>",
                    "--text-file",
                    "<ORIGINAL_TASK_FILE>",
                    "--project-context",
                    "<ORIGINAL_CONTEXT_JSON>",
                    "--note",
                    "Host-affinity override executed native host cell",
                    "--json",
                ],
                "close_command_template": [
                    "agr",
                    "close",
                    planned_route_id,
                    "--obs",
                    "<EXECUTION_ID>",
                    "--json",
                ],
            }

        if fallback is not None:
            fallback["feedback"] = {
                "mode": "raw_native",
                "decision_id": None,
                "unexecuted_decision_id": decision_id,
                "model": qualified_target_model,
                "effort": target_effort,
                "command_template": [
                    "howhard", "observe", "<TASK>", qualified_target_model, target_effort,
                    "<clean|loud|silent|interrupted>", "--native", "--id", "<EXECUTION_ID>",
                    "--text-file", "<ORIGINAL_TASK_FILE>",
                    "--project-context", "<ORIGINAL_CONTEXT_JSON>", "--json",
                ],
                "close_command_template": [
                    "agr", "close", planned_route_id, "--obs", "<EXECUTION_ID>", "--json",
                ],
            }

        result = {
            "format": PLAN_FORMAT,
            "evaluated_at": evaluated_iso,
            "host_agent": host,
            "target_agent": target_agent,
            "execution_mode": execution_mode,
            "decision_id": decision_id,
            "cap_met": _decision_selection_flag(decision, "cap_met"),
            "needs_review": bool(_decision_selection_flag(decision, "needs_review", False)),
            "cost_policy": decision.get("cost_policy"),
            "howhard_cell": {
                "model": qualified_target_model,
                "effort": target_effort,
            },
            "resolved_target": {
                "agent": target_agent,
                "model": target_model,
                "effort": target_effort,
                "surface": meta["surface"],
            },
            "rate_card": price_fields.get("rate_card"),
            "host_affinity_applied": host_affinity_applied,
            "host_affinity_detail": affinity_detail,
            "invocation": invocation,
            "fallback": fallback,
            "feedback": feedback,
            "warnings": warnings,
        }
        if "usd_per_mtok" in price_fields:
            result["usd_per_mtok"] = price_fields["usd_per_mtok"]
        if live_budget is not None:
            result["credit_budget"] = deepcopy(live_budget)
        if needed_credit is not None:
            result["needed_credit_usd"] = needed_credit
        if session_cost is not None:
            result["session_cost_usd"] = session_cost
        result["cost_per_credit"] = cell_cost_per_credit
        if meta.get("monthly_cost_usd") is not None:
            result["monthly_cost_usd"] = meta["monthly_cost_usd"]
        if meta.get("weekly_cost_usd") is not None:
            result["weekly_cost_usd"] = meta["weekly_cost_usd"]
        if needed_credit is not None and needed_credit > 0.0 and reservation_pools:
            # Re-verify against live reservations and record atomically under the usage lock.
            with _usage_locked(self.config):
                live_store = read_store(self.config)
                live_eff = visible_agent_state(
                    target_agent, live_store["agents"][target_agent], self.config.agents[target_agent], now_dt
                )
                live_reserved = active_reserved_credit_by_pool(self.config, target_agent, live_eff, now_dt)
                live_cal_store = read_calibration_store(self.config)
                live_mq = resolve_model_quota(
                    target_agent,
                    self.config.agents[target_agent],
                    live_eff,
                    meta["candidate"],
                    is_host=(target_agent == host),
                    surface=meta["surface"],
                    calibration_store=live_cal_store,
                    reserved_credit_by_pool=live_reserved,
                )
                if not live_mq["eligible"]:
                    raise AgRouteError(
                        f"plan: chosen cell ({qualified_target_model!r}, {target_effort!r}) is not an eligible route "
                        f"for host '{host}' ({live_mq['exclusion_reason']})"
                    )
                locked_budget = live_mq.get("credit_budget")
                locked_reserves = {
                    window: live_mq["credit_calibration"][window]["reserve_credit_usd"]
                    for window in ("session_5h", "weekly_7d")
                    if f"{window}_remaining_credit_usd" in (locked_budget or {})
                }
                locked_reserved = {
                    window: live_mq["credit_calibration"][window].get("reserved_credit_usd", 0.0)
                    for window in ("session_5h", "weekly_7d")
                    if f"{window}_remaining_credit_usd" in (locked_budget or {})
                }
                _check_credit_fit(locked_budget, locked_reserves, locked_reserved)
                if locked_budget is not None:
                    result["credit_budget"] = deepcopy(locked_budget)
                route_id = record_plan_route(
                    self.config,
                    result,
                    locked=True,
                    route_id=planned_route_id,
                    task_text=prompt,
                )
                record_credit_reservation_locked(
                    self.config,
                    route_id=route_id,
                    decision_id=decision_id,
                    agent=target_agent,
                    model=target_model,
                    pools=live_mq.get("reservation_pools") or reservation_pools,
                    reserved_credit_usd=needed_credit,
                    created_at=result["evaluated_at"],
                    now_dt=now_dt,
                )
                result["route_id"] = route_id
        else:
            result["route_id"] = record_plan_route(
                self.config,
                result,
                route_id=planned_route_id,
                task_text=prompt,
            )
        return result

    def close_route(
        self,
        identifier,
        *,
        status=None,
        obs=None,
        switched_from=None,
        superseded_by=None,
        switched_to=None,
        effort=None,
        tokens=None,
        seconds=None,
        credit_usd=None,
        note=None,
        now=None,
    ):
        return close_plan_route(
            self.config,
            identifier,
            status=status,
            obs=obs,
            switched_from=switched_from,
            superseded_by=superseded_by,
            switched_to=switched_to,
            effort=effort,
            tokens=tokens,
            seconds=seconds,
            credit_usd=credit_usd,
            note=note,
            now=now,
        )

    def snapshot(self, *, all_repos=True, now=None):
        return build_snapshot(self, all_repos=all_repos, now=now)

    def list_routes(
        self,
        *,
        all_repos=True,
        repo=None,
        agent=None,
        state=None,
        limit=None,
        now=None,
    ):
        snap = self.snapshot(all_repos=all_repos, now=now)
        routes = list(snap["routes"])
        if repo is not None:
            needle = text(repo, "repo").strip()
            routes = [
                r
                for r in routes
                if needle in (r["repository_id"], r["repository"], r["repository"].rsplit("/", 1)[-1])
            ]
        if agent is not None:
            ag = agent_id(agent, "agent")
            routes = [r for r in routes if r["target_agent"] == ag]
        if state is not None:
            st = text(state, "state", label=True, max_length=32)
            routes = [
                r
                for r in routes
                if st in (r.get("status"), r.get("route_status"), r.get("outcome"))
            ]
        if limit is not None:
            if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
                raise AgRouteError("limit: expected a positive integer")
            routes = routes[:limit]
        return {
            **snap,
            "routes": routes,
        }

    def show_route(self, identifier, *, all_repos=True, now=None):
        snap = self.snapshot(all_repos=all_repos, now=now)
        return find_route(snap, identifier)

    def models(self, *, refresh=False, now=None):
        now_dt = _now_utc(now)
        store = read_store(self.config)
        cal_store = read_calibration_store(self.config)
        out = {}
        for name in AGENTS:
            cfg = self.config.agents[name]
            eff = effective_agent_state(store["agents"][name], now_dt)
            reserved_by_pool = active_reserved_credit_by_pool(self.config, name, eff, now_dt)
            effective_plan, _ = resolve_effective_plan_credit(
                name, cfg, eff.get("detected_plan")
            )
            nat_cfg = cfg["native_candidates"]
            cli_cfg = cfg["cli_candidates"]
            out[name] = {
                "enabled": cfg["enabled"],
                "plan": cfg["plan"],
                "detected_plan": eff.get("detected_plan"),
                "effective_plan": effective_plan,
                "native_source": "configured" if nat_cfg else "discovered",
                "native_candidates": deepcopy(nat_cfg) if nat_cfg else discover_agent_candidates(name, "native", refresh=refresh),
                "cli_source": "configured" if cli_cfg else "discovered",
                "cli_candidates": deepcopy(cli_cfg) if cli_cfg else discover_agent_candidates(name, "cli", refresh=refresh),
                "models": self._agent_model_quotas(
                    name,
                    cfg,
                    eff,
                    calibration_store=cal_store,
                    reserved_credit_by_pool=reserved_by_pool,
                ),
            }
        return {
            "format": "agroute-models-v1",
            "agents": out,
        }

    def config_show(self, *, now=None):
        now_dt = _now_utc(now)
        store = read_store(self.config)
        agents_out = {}
        for name in AGENTS:
            cfg = {
                k: v
                for k, v in deepcopy(self.config.agents[name]).items()
                if k != "credit_overrides"
            }
            eff = effective_agent_state(store["agents"][name], now_dt)
            effective_plan, effective_credit = resolve_effective_plan_credit(
                name, self.config.agents[name], eff.get("detected_plan")
            )
            agents_out[name] = {
                **cfg,
                "detected_plan": eff.get("detected_plan"),
                "effective_plan": effective_plan,
                "effective_credit": effective_credit,
            }
        return {
            "format": "agroute-config-v1",
            "config_path": str(self.config.config_path) if self.config.config_path else None,
            "user_config_path": str(self.config.user_config_path) if self.config.user_config_path else None,
            "routing": {
                "default_host": self.config.default_host,
                "host_affinity_epsilon": self.config.host_affinity_epsilon,
                "namespace_separator": self.config.namespace_separator,
            },
            "agents": agents_out,
        }

    def config_set_plan(self, agent, plan, *, scope="user", config_path=None):
        res = set_agent_plan_in_config(
            agent,
            plan,
            config_path=config_path,
            project=self.config.capture_root,
            scope=scope,
        )
        self.config = load_config(
            self.config.config_path,
            project=self.config.capture_root,
            state_directory=self.config.state_root if self.config.explicit_state else None,
            include_user_config=True,
        )
        return res

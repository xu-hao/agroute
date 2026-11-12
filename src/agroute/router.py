"""Core candidate filtering, howhard inventory generation, and execution plan resolution."""

from copy import deepcopy
import json
import re

from .adapters import get_adapter
from .config import load_config
from .discovery import discover_agent_candidates
from .price_refresh import read_snapshot, refresh as refresh_prices
from .pricing import rate_card, resolve_price, user_rate_card
from .routes import build_snapshot, find_route, record_plan_route, register_repository
from .usage import (
    _now_utc,
    apply_usage_patch,
    clear_usage,
    effective_agent_state,
    ensure_fresh_usage,
    evaluate_agent_quota,
    evaluate_candidate_tier,
    parse_native_usage,
    probe_usage,
    read_store,
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


def _toml_value(value):
    """Render a candidate cell value as an inline TOML value."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, dict):
        items = (f"{k if re.fullmatch(r'[A-Za-z0-9_-]+', k) else json.dumps(k)} = {_toml_value(v)}"
                 for k, v in value.items())
        return "{ " + ", ".join(items) + " }"
    raise AgRouteError(f"inventory: cannot render {type(value).__name__} as TOML")


def _extract_cell_score(entry):
    for key in ("J", "expected_cost", "sampled_cost", "cost"):
        val = entry.get(key)
        if type(val) in (int, float):
            return float(val)
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

    def _agent_model_quotas(self, name, cfg, eff):
        nat_cands = deepcopy(cfg["native_candidates"]) if cfg["native_candidates"] else discover_agent_candidates(name, "native")
        cli_cands = deepcopy(cfg["cli_candidates"]) if cfg["cli_candidates"] else discover_agent_candidates(name, "cli")
        models_map = {}
        for surface, cands, is_host in (("cli", cli_cands, False), ("native", nat_cands, True)):
            for cand in cands:
                mid = cand["model"]
                q = resolve_model_quota(name, cfg, eff, cand, is_host=is_host, surface=surface)
                if mid not in models_map:
                    models_map[mid] = {
                        "surface": surface,
                        "efforts": [cand["effort"]],
                        "quota_group": q["quota_group"],
                        "session_5h": q["session_5h"],
                        "weekly_7d": q["weekly_7d"],
                        "eligible": q["eligible"],
                        "exclusion_reason": q["exclusion_reason"],
                    }
                else:
                    if cand["effort"] not in models_map[mid]["efforts"]:
                        models_map[mid]["efforts"].append(cand["effort"])
        return models_map

    def usage_show(self, agent=None, *, now=None):
        now_dt = _now_utc(now)
        store = read_store(self.config)
        names = [agent_id(agent, "agent")] if agent is not None else list(AGENTS)
        agents_out = {}
        warnings = []
        for name in names:
            cfg = self.config.agents[name]
            warnings.extend(usage_warnings(name, store["agents"][name], cfg, now_dt))
            eff = visible_agent_state(name, store["agents"][name], cfg, now_dt)
            ok_target, reason_target = evaluate_agent_quota(name, cfg, eff, is_host=False)
            ok_host, reason_host = evaluate_agent_quota(name, cfg, eff, is_host=True)
            agents_out[name] = {
                **eff,
                "enabled": cfg["enabled"],
                "eligible_as_target": ok_target,
                "target_exclusion_reason": reason_target,
                "eligible_as_host_native": ok_host,
                "host_native_exclusion_reason": reason_host,
                "models": self._agent_model_quotas(name, cfg, eff),
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
        if rate_limited is not None:
            patch["rate_limited"] = boolean(rate_limited, "rate_limited")
        if cooldown_until is not None:
            patch["cooldown_until"] = cooldown_until
        if tier is not None or tier_used_pct is not None or tier_reset_at is not None:
            if tier is None:
                raise AgRouteError("usage set: --tier is required when setting tier usage or reset")
            text(tier, "tier", label=True, max_length=64)
            patch["tiers"] = {tier: {"used_pct": tier_used_pct, "reset_at": tier_reset_at}}
        if not patch:
            raise AgRouteError("usage set: at least one quota or cooldown field must be specified")
        updated = apply_usage_patch(self.config, agent, patch, source="set", now=now)
        return {"agent": agent, "usage": updated}

    def usage_ingest(self, agent, raw_text, *, now=None):
        agent_id(agent, "agent")
        patch = parse_native_usage(agent, raw_text)
        updated = apply_usage_patch(self.config, agent, patch, source="ingest", now=now)
        return {"agent": agent, "usage": updated}

    def usage_probe(self, agent=None, *, now=None):
        probed = probe_usage(self.config, agent, now=now)
        return {"probed": probed, "warnings": [warning for state in probed.values() for warning in state.get("probe", {}).get("warnings", [])]}

    def usage_clear(self, agent=None):
        cleared = clear_usage(self.config, agent)
        return {"cleared": cleared}

    def _collect_eligible(self, host, *, include_agents=None, now=None):
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

        store, refresh_failures = ensure_fresh_usage(self.config, agent_order, now=now)
        now_dt = _now_utc(now)

        admitted = []
        admitted_meta = {}
        excluded = []
        warnings = []
        snapshot = read_snapshot(self.config)

        for name in agent_order:
            cfg = self.config.agents[name]
            adapter = get_adapter(name)
            warnings.extend(usage_warnings(name, store["agents"][name], cfg, now_dt))
            eff = visible_agent_state(name, store["agents"][name], cfg, now_dt)
            is_host = name == host
            ok_agent, reason_agent = evaluate_agent_quota(name, cfg, eff, is_host=is_host)
            freshness = routing_freshness_reason(store["agents"][name], cfg, now_dt)
            if cfg["enabled"] and freshness and name not in refresh_failures:
                refresh_failures[name] = (f"{name} quota refresh infrastructure incident at admission: {freshness}; "
                                          f"retry with agroute usage probe --agent {name}")
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
            source_list = cfg["native_candidates"] if is_host else cfg["cli_candidates"]
            if not source_list:
                source_list = discover_agent_candidates(name, surface_name)
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

            for cand in source_list:
                mq = resolve_model_quota(name, cfg, eff, cand, is_host=is_host, surface=surface_name)
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
                cell.update(self._price(name, cfg, adapter, cand, surface_name, qualified, now_dt, snapshot))
                admitted.append(cell)
                admitted_meta[(name, cand["model"], cand["effort"])] = {
                    "qualified_model": qualified,
                    "surface": surface_name,
                    "candidate": deepcopy(cand),
                }

        if not admitted:
            reasons = "; ".join(f"{e['agent']}: {e['reason']}" for e in excluded) or "none configured"
            error = AgRouteError(f"candidates: no eligible candidate routes available ({reasons})")
            error.details = {"format": "agroute-candidates-v1", "host_agent": host, "evaluated_at": format_timestamp(now_dt), "candidates": [], "excluded": excluded, "warnings": warnings}
            raise error
        return host, now_dt, admitted, admitted_meta, excluded, warnings

    @staticmethod
    def _price(name, cfg, adapter, cand, surface, qualified, now_dt, snapshot):
        """Precedence: candidate price, per-model price, later of snapshot and built-in, none."""
        if "usd_per_mtok" in cand:
            return {"usd_per_mtok": cand["usd_per_mtok"]}
        if "rate_card" in cand:
            return {"rate_card": deepcopy(cand["rate_card"])}
        price = cfg["prices"].get(cand["model"])
        if price is not None:
            if "usd_per_mtok" in price:
                return {"usd_per_mtok": price["usd_per_mtok"]}
            return {"rate_card": user_rate_card(price, name, qualified, now_dt)}
        resolved = resolve_price(adapter.rate_card_key(cand["model"], cand["effort"], surface), now_dt, snapshot)
        return {"rate_card": rate_card(resolved, qualified)} if resolved is not None else {}

    def prices_refresh(self, *, now=None):
        """Fetch the price source once (bounded retries) and store a new snapshot."""
        return refresh_prices(self.config, _now_utc(now))

    def candidates(self, host=None, *, include_agents=None, now=None):
        host, now_dt, admitted, _, excluded, warnings = self._collect_eligible(
            host, include_agents=include_agents, now=now
        )
        return {
            "format": "agroute-candidates-v1",
            "host_agent": host,
            "evaluated_at": format_timestamp(now_dt),
            "candidates": admitted,
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
            "excluded": res["excluded"],
            "warnings": res["warnings"],
        }

    def format_inventory_toml(self, host=None, *, name="router", include_agents=None, now=None):
        inv = self.inventory(host, name=name, include_agents=include_agents, now=now)
        cells = inv["inventories"][name]
        lines = ["[inventories]", f"{name} = ["]
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
        host, now_dt, _, admitted_meta, excluded, warnings = self._collect_eligible(
            host, include_agents=include_agents, now=now
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

        epsilon = self.config.host_affinity_epsilon
        if epsilon > 0.0 and chosen_agent != host:
            panel = None
            for panel_key in ("candidates", "cells", "ranking"):
                if isinstance(decision.get(panel_key), list):
                    panel = decision[panel_key]
                    break
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
                    ):
                        if best_native is None or score < best_native[0]:
                            best_native = (score, ag, nat_m, eff_raw)
                if chosen_score is not None and best_native is not None:
                    native_score, nat_ag, nat_m, nat_eff = best_native
                    if native_score <= chosen_score + epsilon:
                        target_agent = nat_ag
                        target_model = nat_m
                        target_effort = nat_eff
                        host_affinity_applied = True
                        affinity_detail = {
                            "chosen_score": chosen_score,
                            "native_score": native_score,
                            "delta": native_score - chosen_score,
                            "epsilon": epsilon,
                        }

        meta = admitted_meta[(target_agent, target_model, target_effort)]
        qualified_target_model = meta["qualified_model"]
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
                    "<clean|loud|silent>",
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
                    "<clean|loud|silent>",
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
            }

        result = {
            "format": PLAN_FORMAT,
            "evaluated_at": format_timestamp(now_dt),
            "host_agent": host,
            "target_agent": target_agent,
            "execution_mode": execution_mode,
            "decision_id": decision_id,
            "cap_met": decision.get("cap_met"),
            "needs_review": bool(decision.get("needs_review", False)),
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
            "host_affinity_applied": host_affinity_applied,
            "host_affinity_detail": affinity_detail,
            "invocation": invocation,
            "fallback": fallback,
            "feedback": feedback,
            "warnings": warnings,
        }
        result["route_id"] = record_plan_route(self.config, result)
        return result

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
            routes = [r for r in routes if r["status"] == st]
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
        out = {}
        for name in AGENTS:
            cfg = self.config.agents[name]
            eff = effective_agent_state(store["agents"][name], now_dt)
            nat_cfg = cfg["native_candidates"]
            cli_cfg = cfg["cli_candidates"]
            out[name] = {
                "enabled": cfg["enabled"],
                "native_source": "configured" if nat_cfg else "discovered",
                "native_candidates": deepcopy(nat_cfg) if nat_cfg else discover_agent_candidates(name, "native", refresh=refresh),
                "cli_source": "configured" if cli_cfg else "discovered",
                "cli_candidates": deepcopy(cli_cfg) if cli_cfg else discover_agent_candidates(name, "cli", refresh=refresh),
                "models": self._agent_model_quotas(name, cfg, eff),
            }
        return {
            "format": "agroute-models-v1",
            "agents": out,
        }

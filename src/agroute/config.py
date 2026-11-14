"""Layered configuration loading and strict validation."""

from copy import deepcopy
from dataclasses import dataclass
from importlib.resources import files
import os
from pathlib import Path
import re
import tomllib

from .adapters import get_adapter
from .files import atomic_write
from .pricing import RATE_CATEGORIES
from .state import roots
from .validation import (
    AGENTS,
    AgRouteError,
    agent_id,
    boolean,
    effort_id,
    keys,
    number,
    parse_timestamp,
    percentage,
    table,
    text,
)

PLAN_PRESETS = {
    "agy": {
        "free": {
            "monthly_cost_usd": 0.0,
            "credit": {"tiers": {"gemini": {"session_5h_credit_usd": 3.0, "weekly_7d_credit_usd": 10.0}}},
        },
        "plus-8": {
            "monthly_cost_usd": 8.0,
            "credit": {"tiers": {"gemini": {"session_5h_credit_usd": 12.0, "weekly_7d_credit_usd": 50.0}}},
        },
        "pro-20": {
            "monthly_cost_usd": 20.0,
            "credit": {
                "tiers": {
                    "gemini": {"session_5h_credit_usd": 32.0, "weekly_7d_credit_usd": 135.0},
                    "3p": {"session_5h_credit_usd": 5.0, "weekly_7d_credit_usd": 10.0},
                },
            },
        },
        "ultra-100": {
            "monthly_cost_usd": 100.0,
            "credit": {
                "tiers": {
                    "gemini": {"session_5h_credit_usd": 80.0, "weekly_7d_credit_usd": 337.5},
                    "3p": {"session_5h_credit_usd": 12.5, "weekly_7d_credit_usd": 25.0},
                },
            },
        },
        "ultra-200": {
            "monthly_cost_usd": 200.0,
            "credit": {
                "tiers": {
                    "gemini": {"session_5h_credit_usd": 320.0, "weekly_7d_credit_usd": 1350.0},
                    "3p": {"session_5h_credit_usd": 25.0, "weekly_7d_credit_usd": 50.0},
                },
            },
        },
    },
    "claude": {
        "pro-20": {
            "monthly_cost_usd": 20.0,
            "credit": {"session_5h_credit_usd": 18.0, "weekly_7d_credit_usd": 45.0, "tiers": {}},
        },
        "max-5x": {
            "monthly_cost_usd": 100.0,
            "credit": {
                "session_5h_credit_usd": 90.0,
                "weekly_7d_credit_usd": 225.0,
                "tiers": {"fable": {"weekly_7d_credit_usd": 75.0}},
            },
        },
        "max-20x": {
            "monthly_cost_usd": 200.0,
            "credit": {
                "session_5h_credit_usd": 360.0,
                "weekly_7d_credit_usd": 900.0,
                "tiers": {"fable": {"weekly_7d_credit_usd": 300.0}},
            },
        },
    },
    "codex": {
        "plus-20": {
            "monthly_cost_usd": 20.0,
            "credit": {"session_5h_credit_usd": 20.0, "weekly_7d_credit_usd": 50.0, "tiers": {}},
        },
        "pro-200": {
            "monthly_cost_usd": 200.0,
            "credit": {"session_5h_credit_usd": 200.0, "weekly_7d_credit_usd": 500.0, "tiers": {}},
        },
        "pro-500": {
            "monthly_cost_usd": 500.0,
            "credit": {"session_5h_credit_usd": 500.0, "weekly_7d_credit_usd": 1250.0, "tiers": {}},
        },
    },
}

PLAN_ALIASES = {
    "agy": {
        "free": "free",
        "google-ai-free": "free",
        "google ai free": "free",
        "plus": "plus-8",
        "google-ai-plus": "plus-8",
        "google ai plus": "plus-8",
        "pro": "pro-20",
        "google-ai-pro": "pro-20",
        "google ai pro": "pro-20",
        "ultra": "ultra-200",
        "google-ai-ultra": "ultra-200",
        "google ai ultra": "ultra-200",
    },
    "claude": {
        "pro": "pro-20",
        "claude-pro": "pro-20",
        "max_5x": "max-5x",
        "claude-max-5x": "max-5x",
        "max_20x": "max-20x",
        "claude-max-20x": "max-20x",
    },
    "codex": {
        "plus": "plus-20",
        "chatgpt-plus": "plus-20",
        "pro": "pro-200",
        "promax": "pro-500",
        "chatgpt-pro": "pro-200",
    },
}

_AGENT_KEYS = (
    "enabled",
    "max_session_used_pct",
    "max_weekly_used_pct",
    "max_tier_used_pct",
    "host_reserve_pct",
    "usage_probe_cmd",
    "usage_stale_after_seconds",
    "native_candidates",
    "cli_candidates",
    "prices",
    "plan",
    "credit",
    "exclude_quota_groups",
)
_AGENT_OPTIONAL_KEYS = ("monthly_cost_usd",)


@dataclass
class Config:
    capture_root: Path
    state_root: Path
    identity_path: Path
    project_identity: str
    config_path: Path | None
    user_config_path: Path | None
    default_host: str
    host_affinity_epsilon: float
    namespace_separator: str
    agents: dict
    explicit_state: bool = False


def _validate_rate_card(card, where):
    keys(
        card,
        ["format", "source", "version", "effective_at", "provider", "model", "service_tier", "basis", "rates"],
        where=where,
    )
    if card["format"] != "howhard-rate-card-v1" or card["basis"] not in ("estimate", "charge"):
        raise AgRouteError(f"{where}: unsupported format or basis")
    for key in ("source", "version", "provider", "model"):
        text(card[key], f"{where}.{key}")
    if card["service_tier"] is not None:
        text(card["service_tier"], f"{where}.service_tier")
    parse_timestamp(card["effective_at"], f"{where}.effective_at")
    rates = table(card["rates"], f"{where}.rates")
    keys(rates, RATE_CATEGORIES, where=f"{where}.rates")
    for category, amount in rates.items():
        if amount is not None:
            number(amount, f"{where}.rates.{category}", nonnegative=True)
    return deepcopy(card)


def _validate_candidate_list(items, where, *, agent):
    if not isinstance(items, list):
        raise AgRouteError(f"{where}: expected a list of candidate tables")
    seen = set()
    validated = []
    for index, raw in enumerate(items):
        entry_where = f"{where}[{index}]"
        keys(
            raw,
            ["model", "effort"],
            ["tier", "usd_per_mtok", "rate_card"],
            where=entry_where,
        )
        model = text(raw["model"], f"{entry_where}.model", max_length=200)
        if "/" in model or ":" in model:
            raise AgRouteError(
                f"{entry_where}.model: configure bare native model ID '{model}' without agent prefix"
            )
        effort = effort_id(raw["effort"], f"{entry_where}.effort")
        if where.endswith("native_candidates"):
            get_adapter(agent).validate_native_candidate(model, f"{entry_where}.model")
        pair = (model, effort)
        if pair in seen:
            raise AgRouteError(f"{entry_where}: duplicate candidate cell ({model!r}, {effort!r})")
        seen.add(pair)
        if "usd_per_mtok" in raw and "rate_card" in raw and raw["usd_per_mtok"] is not None and raw["rate_card"] is not None:
            raise AgRouteError(f"{entry_where}: candidate cannot specify both usd_per_mtok and rate_card")
        cell = {"model": model, "effort": effort}
        if raw.get("tier") is not None:
            cell["tier"] = text(raw["tier"], f"{entry_where}.tier", label=True, max_length=64)
        if raw.get("usd_per_mtok") is not None:
            cell["usd_per_mtok"] = number(raw["usd_per_mtok"], f"{entry_where}.usd_per_mtok", positive=True)
        if raw.get("rate_card") is not None:
            cell["rate_card"] = _validate_rate_card(raw["rate_card"], f"{entry_where}.rate_card")
        validated.append(cell)
    return validated


def _validate_prices(raw, where):
    """Per-model prices: each entry is `usd_per_mtok` alone or all six rate categories."""
    prices = {}
    for model, entry in table(raw, where).items():
        entry_where = f'{where}."{model}"'
        if "/" in model or ":" in model or not model or model.strip() != model or len(model) > 200:
            raise AgRouteError(f"{entry_where}: configure a bare model ID without agent prefix")
        if not isinstance(entry, dict):
            raise AgRouteError(f"{entry_where}: expected a table")
        if "usd_per_mtok" in entry:
            if len(entry) > 1:
                raise AgRouteError(
                    f"{entry_where}: usd_per_mtok cannot be combined with {sorted(set(entry) - {'usd_per_mtok'})}; "
                    "give usd_per_mtok alone or all six rate categories"
                )
            prices[model] = {"usd_per_mtok": number(entry["usd_per_mtok"], f"{entry_where}.usd_per_mtok", positive=True)}
        else:
            keys(entry, RATE_CATEGORIES, where=entry_where)
            prices[model] = {c: number(entry[c], f"{entry_where}.{c}", nonnegative=True) for c in RATE_CATEGORIES}
    return prices


def normalize_plan_slug(agent: str, raw: str | None) -> str | None:
    """Normalize a preset or provider telemetry plan string to its canonical slug for `agent`."""
    if not isinstance(raw, str):
        return None
    norm = raw.strip().lower()
    if not norm:
        return None
    presets = PLAN_PRESETS.get(agent, {})
    aliases = PLAN_ALIASES.get(agent, {})
    if norm in presets:
        return norm
    if norm in aliases:
        return aliases[norm]
    collapsed = re.sub(r"[\s_]+", "-", norm)
    if collapsed in presets:
        return collapsed
    if collapsed in aliases:
        return aliases[collapsed]
    return None


def _merge_credit_dict(base_credit: dict, override_credit: dict) -> dict:
    effective = deepcopy(base_credit)
    effective.setdefault("tiers", {})
    for ck in ("session_5h_credit_usd", "weekly_7d_credit_usd"):
        if ck in override_credit:
            effective[ck] = override_credit[ck]
    for tk, tv in (override_credit.get("tiers") or {}).items():
        effective["tiers"][tk] = {
            **effective["tiers"].get(tk, {}),
            **deepcopy(tv),
        }
    return effective


def resolve_plan_credit_sources(
    agent_name: str,
    agent_cfg: dict,
    detected_plan: str | None,
) -> tuple[str, dict, dict, dict]:
    """Return `(effective_plan, preset_credit, manual_credit, effective_credit)` for `agent_name`."""
    configured = (agent_cfg.get("plan") or "").strip().lower()
    has_overrides_key = "credit_overrides" in agent_cfg
    raw_credit = deepcopy(agent_cfg.get("credit") or {"tiers": {}})
    raw_credit.setdefault("tiers", {})
    if has_overrides_key:
        manual_credit = deepcopy(agent_cfg.get("credit_overrides") or {"tiers": {}})
        manual_credit.setdefault("tiers", {})
    else:
        manual_credit = deepcopy(raw_credit)

    if configured in ("", "none"):
        return "", {"tiers": {}}, manual_credit, _merge_credit_dict({"tiers": {}}, manual_credit)
    if configured == "auto":
        canonical = normalize_plan_slug(agent_name, detected_plan)
        if canonical is None:
            return "", {"tiers": {}}, manual_credit, _merge_credit_dict({"tiers": {}}, manual_credit)
        preset = deepcopy(PLAN_PRESETS[agent_name][canonical]["credit"])
        preset.setdefault("tiers", {})
        return canonical, preset, manual_credit, _merge_credit_dict(preset, manual_credit)

    canonical = normalize_plan_slug(agent_name, configured) or configured
    preset = deepcopy(PLAN_PRESETS.get(agent_name, {}).get(canonical, {}).get("credit", {"tiers": {}}))
    preset.setdefault("tiers", {})
    if not has_overrides_key:
        # Infer manual overrides when a synthetic dict omitted `credit_overrides`.
        inferred_manual: dict = {"tiers": {}}
        for ck in ("session_5h_credit_usd", "weekly_7d_credit_usd"):
            if ck in raw_credit and raw_credit[ck] != preset.get(ck):
                inferred_manual[ck] = raw_credit[ck]
        for tk, tv in (raw_credit.get("tiers") or {}).items():
            p_tier = (preset.get("tiers") or {}).get(tk, {})
            diff = {k: v for k, v in tv.items() if v != p_tier.get(k)}
            if diff:
                inferred_manual["tiers"][tk] = diff
        manual_credit = inferred_manual
    return canonical, preset, manual_credit, _merge_credit_dict(preset, manual_credit)


def resolve_effective_plan_credit(
    agent_name: str,
    agent_cfg: dict,
    detected_plan: str | None,
) -> tuple[str, dict]:
    """Resolve effective canonical plan slug and merged credit table for an agent."""
    canonical, _, _, merged = resolve_plan_credit_sources(agent_name, agent_cfg, detected_plan)
    return canonical, merged


def resolve_plan_monthly_cost(agent_name: str, agent_cfg: dict, detected_plan: str | None) -> float | None:
    """Resolve subscription cash cost; a missing plan is treated as pay-as-you-go."""
    override = agent_cfg.get("monthly_cost_override")
    if override is not None:
        return float(override)
    configured = (agent_cfg.get("plan") or "").strip().lower()
    raw_plan = detected_plan if configured == "auto" else configured
    canonical = normalize_plan_slug(agent_name, raw_plan)
    if canonical is None:
        return None
    return float(PLAN_PRESETS[agent_name][canonical]["monthly_cost_usd"])


def _validate_plan(raw, where, *, agent):
    if not isinstance(raw, str):
        raise AgRouteError(f"{where}: expected a string")
    norm = raw.strip().lower()
    if norm in ("", "none"):
        return "", {"tiers": {}}
    if norm == "auto":
        return "auto", {"tiers": {}}
    presets = PLAN_PRESETS[agent]
    aliases = PLAN_ALIASES[agent]
    canonical = normalize_plan_slug(agent, raw)
    if canonical is None:
        valid = sorted({"auto"} | set(presets) | set(aliases))
        raise AgRouteError(
            f"{where}: unknown plan {raw!r} for agent '{agent}' (expected one of {valid})"
        )
    return canonical, deepcopy(presets[canonical]["credit"])


def _validate_credit(raw, where):
    table(raw, where)
    raw = _normalize_credit_fields(raw, where)
    keys(raw, [], ["session_5h_credit_usd", "weekly_7d_credit_usd", "tiers"], where=where)
    validated = {"tiers": {}}
    if "session_5h_credit_usd" in raw:
        validated["session_5h_credit_usd"] = number(
            raw["session_5h_credit_usd"], f"{where}.session_5h_credit_usd", positive=True
        )
    if "weekly_7d_credit_usd" in raw:
        validated["weekly_7d_credit_usd"] = number(
            raw["weekly_7d_credit_usd"], f"{where}.weekly_7d_credit_usd", positive=True
        )
    if "tiers" in raw:
        tiers_raw = table(raw["tiers"], f"{where}.tiers")
        for group, tier_raw in tiers_raw.items():
            text(group, f"{where}.tiers.{group}", label=True, max_length=64)
            tier_where = f"{where}.tiers.{group}"
            table(tier_raw, tier_where)
            tier_raw = _normalize_credit_fields(tier_raw, tier_where)
            keys(tier_raw, [], ["session_5h_credit_usd", "weekly_7d_credit_usd"], where=tier_where)
            if not tier_raw:
                raise AgRouteError(
                    f"{tier_where}: expected at least one of session_5h_credit_usd or weekly_7d_credit_usd"
                )
            t_entry = {}
            if "session_5h_credit_usd" in tier_raw:
                t_entry["session_5h_credit_usd"] = number(
                    tier_raw["session_5h_credit_usd"], f"{tier_where}.session_5h_credit_usd", positive=True
                )
            if "weekly_7d_credit_usd" in tier_raw:
                t_entry["weekly_7d_credit_usd"] = number(
                    tier_raw["weekly_7d_credit_usd"], f"{tier_where}.weekly_7d_credit_usd", positive=True
                )
            validated["tiers"][group.lower()] = t_entry
    return validated


def _normalize_credit_fields(raw: dict, where: str) -> dict:
    """Treat former quota-only field names as credit aliases at config input boundaries."""
    normalized = deepcopy(raw)
    for old, new in (
        ("session_5h_usd", "session_5h_credit_usd"),
        ("weekly_7d_usd", "weekly_7d_credit_usd"),
    ):
        if old in normalized and new in normalized:
            raise AgRouteError(f"{where}: cannot combine legacy {old} with {new}")
        if old in normalized:
            normalized[new] = normalized.pop(old)
    return normalized


def _validate_exclude_quota_groups(raw, where):
    if not isinstance(raw, list):
        raise AgRouteError(f"{where}: expected a list of quota-group labels")
    return list(
        dict.fromkeys(
            text(item, where, label=True, max_length=64).lower() for item in raw
        )
    )


def _merge_config(base, override, where):
    table(override, where)
    unknown_top = set(override) - {"routing", "agents", "state_dir"}
    if unknown_top:
        raise AgRouteError(f"{where}: unknown top-level keys {sorted(unknown_top)}")
    merged = deepcopy(base)
    if "state_dir" in override:
        merged["state_dir"] = text(override["state_dir"], f"{where}.state_dir", max_length=1024)
    if "routing" in override:
        r_override = table(override["routing"], f"{where}.routing")
        unknown_r = set(r_override) - {"default_host", "host_affinity_epsilon", "namespace_separator"}
        if unknown_r:
            raise AgRouteError(f"{where}.routing: unknown keys {sorted(unknown_r)}")
        merged["routing"].update(deepcopy(r_override))
    if "agents" in override:
        a_override = table(override["agents"], f"{where}.agents")
        for name, spec in a_override.items():
            agent_id(name, f"{where}.agents.{name}")
            table(spec, f"{where}.agents.{name}")
            unknown_a = set(spec) - set(_AGENT_KEYS) - set(_AGENT_OPTIONAL_KEYS)
            if unknown_a:
                raise AgRouteError(f"{where}.agents.{name}: unknown keys {sorted(unknown_a)}")
            spec = deepcopy(spec)
            if "prices" in spec:
                # A later layer replaces an earlier layer's entry for the same model only.
                spec["prices"] = {**merged["agents"][name]["prices"],
                                  **table(spec["prices"], f"{where}.agents.{name}.prices")}
            if "credit" in spec:
                credit_where = f"{where}.agents.{name}.credit"
                c_override = _normalize_credit_fields(table(spec["credit"], credit_where), credit_where)
                merged_credit = deepcopy(merged["agents"][name].get("credit", {}))
                for ck, cv in c_override.items():
                    if ck == "tiers":
                        t_override = table(cv, f"{credit_where}.tiers")
                        merged_tiers = deepcopy(merged_credit.get("tiers", {}))
                        for tk, tv in t_override.items():
                            tier_where = f"{credit_where}.tiers.{tk}"
                            t_spec = _normalize_credit_fields(table(tv, tier_where), tier_where)
                            merged_tiers[tk] = {**merged_tiers.get(tk, {}), **deepcopy(t_spec)}
                        merged_credit["tiers"] = merged_tiers
                    else:
                        merged_credit[ck] = deepcopy(cv)
                spec["credit"] = merged_credit
            merged["agents"][name].update(spec)
    return merged


def _validate_merged(raw):
    routing = table(raw.get("routing"), "routing")
    keys(routing, ["default_host", "host_affinity_epsilon", "namespace_separator"], where="routing")
    default_host = agent_id(routing["default_host"], "routing.default_host")
    epsilon = number(routing["host_affinity_epsilon"], "routing.host_affinity_epsilon", nonnegative=True)
    separator = text(routing["namespace_separator"], "routing.namespace_separator", max_length=1)
    if separator not in ("/", ":"):
        raise AgRouteError("routing.namespace_separator: expected '/' or ':'")

    agents_raw = table(raw.get("agents"), "agents")
    keys(agents_raw, AGENTS, where="agents")
    agents = {}
    for name in AGENTS:
        where = f"agents.{name}"
        spec = table(agents_raw[name], where)
        keys(spec, _AGENT_KEYS, _AGENT_OPTIONAL_KEYS, where=where)
        enabled = boolean(spec["enabled"], f"{where}.enabled")
        max_session = percentage(spec["max_session_used_pct"], f"{where}.max_session_used_pct")
        max_weekly = percentage(spec["max_weekly_used_pct"], f"{where}.max_weekly_used_pct")
        max_tier = percentage(spec["max_tier_used_pct"], f"{where}.max_tier_used_pct")
        reserve = percentage(spec["host_reserve_pct"], f"{where}.host_reserve_pct")
        if reserve > max_session or reserve > max_weekly:
            raise AgRouteError(f"{where}.host_reserve_pct: cannot exceed max_session_used_pct or max_weekly_used_pct")
        probe_cmd = spec["usage_probe_cmd"]
        if not isinstance(probe_cmd, list) or any(not isinstance(arg, str) or not arg for arg in probe_cmd):
            raise AgRouteError(f"{where}.usage_probe_cmd: expected a list of non-empty strings")
        native_cands = _validate_candidate_list(
            spec["native_candidates"], f"{where}.native_candidates", agent=name
        )
        cli_cands = _validate_candidate_list(
            spec["cli_candidates"], f"{where}.cli_candidates", agent=name
        )
        plan_id, preset_credit = _validate_plan(spec["plan"], f"{where}.plan", agent=name)
        monthly_override = (
            number(spec["monthly_cost_usd"], f"{where}.monthly_cost_usd", nonnegative=True)
            if "monthly_cost_usd" in spec
            else None
        )
        preset_cost = (
            float(PLAN_PRESETS[name][plan_id]["monthly_cost_usd"])
            if plan_id in PLAN_PRESETS[name]
            else None
        )
        monthly_cost = monthly_override if monthly_override is not None else preset_cost
        override_credit = _validate_credit(spec["credit"], f"{where}.credit")
        effective_credit = deepcopy(preset_credit)
        for ck in ("session_5h_credit_usd", "weekly_7d_credit_usd"):
            if ck in override_credit:
                effective_credit[ck] = override_credit[ck]
        for tk, tv in override_credit.get("tiers", {}).items():
            effective_credit.setdefault("tiers", {})[tk] = {
                **effective_credit.get("tiers", {}).get(tk, {}),
                **tv,
            }
        exclude_quota_groups = _validate_exclude_quota_groups(
            spec["exclude_quota_groups"], f"{where}.exclude_quota_groups"
        )
        agents[name] = {
            "enabled": enabled,
            "max_session_used_pct": max_session,
            "max_weekly_used_pct": max_weekly,
            "max_tier_used_pct": max_tier,
            "host_reserve_pct": reserve,
            "usage_probe_cmd": list(probe_cmd),
            "usage_stale_after_seconds": number(spec["usage_stale_after_seconds"], f"{where}.usage_stale_after_seconds", positive=True),
            "native_candidates": native_cands,
            "cli_candidates": cli_cands,
            "prices": _validate_prices(spec["prices"], f"{where}.prices"),
            "plan": plan_id,
            "monthly_cost_usd": monthly_cost,
            "weekly_cost_usd": monthly_cost * 7.0 / 30.0 if monthly_cost is not None else None,
            "monthly_cost_override": monthly_override,
            "credit": effective_credit,
            "credit_overrides": deepcopy(override_credit),
            "exclude_quota_groups": exclude_quota_groups,
        }
    return default_host, epsilon, separator, agents


def _read_toml(path, where):
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise AgRouteError(f"{where}: unable to read {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise AgRouteError(f"{where}: invalid TOML in {path}: {exc}") from exc


def load_config(path=None, *, project=None, state_directory=None, include_user_config=True):
    default_bytes = files("agroute").joinpath("data/default_config.toml").read_text(encoding="utf-8")
    merged = tomllib.loads(default_bytes)

    user_cfg_path = None
    if include_user_config:
        env_user = os.environ.get("AGROUTE_USER_CONFIG")
        if env_user:
            candidate = Path(env_user).expanduser().resolve()
            if not candidate.is_file():
                raise AgRouteError(f"user config: file not found at {candidate}")
            user_cfg_path = candidate
        else:
            xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            candidate = (xdg / "agroute" / "config.toml").resolve()
            if candidate.is_file():
                user_cfg_path = candidate
        if user_cfg_path is not None:
            merged = _merge_config(merged, _read_toml(user_cfg_path, "user config"), "user config")

    explicit = path is not None
    base_dir = Path(project).resolve() if project is not None else Path.cwd().resolve()
    config_path = None
    if explicit:
        config_path = Path(path).expanduser().resolve()
        if not config_path.is_file():
            raise AgRouteError(f"config: file not found at {config_path}")
        if project is None:
            base_dir = config_path.parent
        merged = _merge_config(merged, _read_toml(config_path, "config"), "config")
    else:
        local_candidate = base_dir / "agroute.toml"
        if local_candidate.is_file():
            config_path = local_candidate.resolve()
            merged = _merge_config(merged, _read_toml(config_path, "config"), "config")

    default_host, epsilon, separator, agents = _validate_merged(merged)

    cfg_state_dir = state_directory
    if cfg_state_dir is None and "state_dir" in merged:
        raw_state = Path(merged["state_dir"]).expanduser()
        anchor = config_path.parent if config_path is not None else base_dir
        cfg_state_dir = (anchor / raw_state).resolve() if not raw_state.is_absolute() else raw_state.resolve()

    capture_root, state_root, identity_path, project_identity = roots(
        base_dir,
        explicit=explicit and project is None,
        state_directory=cfg_state_dir,
    )
    return Config(
        capture_root=capture_root,
        state_root=state_root,
        identity_path=identity_path,
        project_identity=project_identity,
        config_path=config_path,
        user_config_path=user_cfg_path,
        default_host=default_host,
        host_affinity_epsilon=epsilon,
        namespace_separator=separator,
        agents=agents,
        explicit_state=bool(cfg_state_dir is not None or os.environ.get("AGROUTE_STATE_DIR")),
    )


_TOML_SECTION_RE = re.compile(
    r'^\s*(\[\[?\s*[A-Za-z0-9_."-]+(?:\s*\.\s*[A-Za-z0-9_."-]+)*\s*\]\]?)\s*(?:#.*)?$'
)
_TOML_PLAN_KEY_RE = re.compile(r"^(\s*)plan\s*=\s*(.*?)(?:\s*(#.*))?$")


def _resolve_default_user_config_path() -> Path:
    env_user = os.environ.get("AGROUTE_USER_CONFIG")
    if env_user:
        return Path(env_user).expanduser().resolve()
    xdg = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return (xdg / "agroute" / "config.toml").resolve()


def _update_agent_plan_toml_text(existing_text: str, agent: str, canonical_plan: str) -> str:
    target_header = f"agents.{agent}"
    lines = existing_text.splitlines()
    if not any(line.strip() for line in lines):
        return f'[agents.{agent}]\nplan = "{canonical_plan}"\n'

    sections: list[tuple[int, str, bool]] = []
    for idx, line in enumerate(lines):
        m = _TOML_SECTION_RE.match(line)
        if not m:
            continue
        raw_hdr = m.group(1).strip()
        is_array = raw_hdr.startswith("[[") and raw_hdr.endswith("]]")
        inner = raw_hdr[2:-2].strip() if is_array else raw_hdr[1:-1].strip()
        norm_path = ".".join(part.strip().strip('"').strip("'") for part in inner.split("."))
        sections.append((idx, norm_path, is_array))

    for sec_i, (hdr_line_idx, norm_path, is_array) in enumerate(sections):
        if not is_array and norm_path == target_header:
            next_hdr_idx = sections[sec_i + 1][0] if sec_i + 1 < len(sections) else len(lines)
            for line_idx in range(hdr_line_idx + 1, next_hdr_idx):
                pm = _TOML_PLAN_KEY_RE.match(lines[line_idx])
                if pm:
                    indent = pm.group(1)
                    comment = f" {pm.group(3)}" if pm.group(3) else ""
                    lines[line_idx] = f'{indent}plan = "{canonical_plan}"{comment}'
                    return "\n".join(lines) + "\n"
            lines.insert(hdr_line_idx + 1, f'plan = "{canonical_plan}"')
            return "\n".join(lines) + "\n"

    for hdr_line_idx, norm_path, _ in sections:
        if norm_path.startswith(f"{target_header}."):
            insert_block = [f"[agents.{agent}]", f'plan = "{canonical_plan}"', ""]
            lines[hdr_line_idx:hdr_line_idx] = insert_block
            return "\n".join(lines) + "\n"

    if lines and lines[-1].strip() != "":
        lines.append("")
    lines.append(f"[agents.{agent}]")
    lines.append(f'plan = "{canonical_plan}"')
    return "\n".join(lines) + "\n"


def set_agent_plan_in_config(
    agent: str,
    raw_plan: str,
    *,
    config_path: str | Path | None = None,
    project: str | Path | None = None,
    scope: str = "user",
) -> dict:
    """Validate and persist `[agents.<agent>].plan` in user or project TOML configuration."""
    agent = agent_id(agent, "agent")
    if scope not in ("user", "project"):
        raise AgRouteError(f"config set-plan: unsupported scope {scope!r} (expected 'user' or 'project')")
    canonical_plan, _ = _validate_plan(raw_plan, f"agents.{agent}.plan", agent=agent)

    if scope == "project":
        if config_path is not None:
            target_path = Path(config_path).expanduser().resolve()
        else:
            base_dir = Path(project).resolve() if project is not None else Path.cwd().resolve()
            target_path = (base_dir / "agroute.toml").resolve()
    else:
        if config_path is not None:
            target_path = Path(config_path).expanduser().resolve()
        else:
            target_path = _resolve_default_user_config_path()

    existing_text = ""
    if target_path.exists():
        try:
            existing_text = target_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AgRouteError(f"config set-plan: unable to read {target_path}: {exc}") from exc

    new_text = _update_agent_plan_toml_text(existing_text, agent, canonical_plan)
    try:
        parsed = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        raise AgRouteError(f"config set-plan: invalid TOML in {target_path}: {exc}") from exc

    default_bytes = files("agroute").joinpath("data/default_config.toml").read_text(encoding="utf-8")
    merged = _merge_config(tomllib.loads(default_bytes), parsed, str(target_path))
    _, _, _, validated_agents = _validate_merged(merged)

    atomic_write(target_path, new_text)
    return {
        "agent": agent,
        "plan": canonical_plan,
        "scope": scope,
        "config_path": str(target_path),
        "credit": deepcopy(validated_agents[agent]["credit"]),
    }

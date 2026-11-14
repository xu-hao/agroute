"""Command-line interface for agroute."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .config import set_agent_plan_in_config
from .files import atomic_write
from .price_store import (
    format_pending_report,
    format_resolve_report,
    format_show_report,
)
from .router import Router
from . import tui
from .validation import AgRouteError


def _add_common_flags(parser):
    parser.add_argument("--config", default=None, help="Path to agroute.toml configuration file")
    parser.add_argument("--state-dir", default=None, help="Explicit state directory override")
    parser.add_argument(
        "--no-user-config",
        action="store_true",
        help="Ignore user-level ~/.config/agroute/config.toml",
    )
    parser.add_argument("--now", default=None, help="Evaluation timestamp override (ISO-8601 UTC)")
    parser.add_argument("--json", action="store_true", help="Emit structured JSON output")


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="agr",
        description="Configurable local agent router across Codex, Claude Code, and Antigravity.",
    )
    parser.add_argument("--version", action="version", version=f"agr {__version__}")
    _add_common_flags(parser)

    subparsers = parser.add_subparsers(dest="command")

    # paths
    p_paths = subparsers.add_parser("paths", help="Show resolved capture and state roots")
    _add_common_flags(p_paths)

    # config
    p_config = subparsers.add_parser("config", help="Inspect or update agroute configuration")
    _add_common_flags(p_config)
    config_sub = p_config.add_subparsers(dest="config_command")

    p_c_show = config_sub.add_parser("show", help="Show active merged configuration and plan resolution")
    _add_common_flags(p_c_show)

    p_c_set_plan = config_sub.add_parser(
        "set-plan", help="Set subscription plan preset ('auto', 'none', or preset name) for an agent"
    )
    p_c_set_plan.add_argument("agent", help="Target agent (codex|claude|agy)")
    p_c_set_plan.add_argument("plan", help="Plan name ('auto', 'none'/'', or preset/alias)")
    scope_group = p_c_set_plan.add_mutually_exclusive_group()
    scope_group.add_argument(
        "--user",
        action="store_true",
        help="Write to user config ~/.config/agroute/config.toml (default)",
    )
    scope_group.add_argument(
        "--project",
        action="store_true",
        help="Write to project agroute.toml in the capture root",
    )
    _add_common_flags(p_c_set_plan)

    # usage
    p_usage = subparsers.add_parser("usage", help="Inspect or update native agent quota and rate-limit state")
    _add_common_flags(p_usage)
    usage_sub = p_usage.add_subparsers(dest="usage_command")

    p_u_show = usage_sub.add_parser("show", help="Show effective quota and eligibility per agent")
    p_u_show.add_argument("--agent", default=None, help="Filter to a single agent (codex|claude|agy)")
    _add_common_flags(p_u_show)

    p_u_set = usage_sub.add_parser("set", help="Explicitly set quota window percentages or cooldowns")
    p_u_set.add_argument("--agent", required=True, help="Target agent (codex|claude|agy)")
    p_u_set.add_argument("--session-used-pct", type=float, default=None, help="5h session used percentage (0-100)")
    p_u_set.add_argument("--session-reset-at", default=None, help="5h session reset timestamp (ISO-8601 or epoch)")
    p_u_set.add_argument("--weekly-used-pct", type=float, default=None, help="7d weekly used percentage (0-100)")
    p_u_set.add_argument("--weekly-reset-at", default=None, help="7d weekly reset timestamp (ISO-8601 or epoch)")
    p_u_set.add_argument("--tier", default=None, help="Model tier name (e.g. opus, sonnet, pro, flash)")
    p_u_set.add_argument("--tier-used-pct", type=float, default=None, help="Tier used percentage (0-100)")
    p_u_set.add_argument("--tier-reset-at", default=None, help="Tier reset timestamp (ISO-8601 or epoch)")
    rl_group = p_u_set.add_mutually_exclusive_group()
    rl_group.add_argument("--rate-limited", dest="rate_limited", action="store_true", default=None)
    rl_group.add_argument("--no-rate-limited", dest="rate_limited", action="store_false")
    p_u_set.add_argument("--cooldown-until", default=None, help="Rate-limit cooldown expiry timestamp")
    _add_common_flags(p_u_set)

    p_u_ingest = usage_sub.add_parser("ingest", help="Parse native usage/status output for an agent")
    p_u_ingest.add_argument("--agent", required=True, help="Target agent (codex|claude|agy)")
    p_u_ingest.add_argument("--file", default=None, help="Path to captured native status/event output")
    p_u_ingest.add_argument("--text", default=None, help="Inline native status/event text")
    _add_common_flags(p_u_ingest)

    p_u_probe = usage_sub.add_parser("probe", help="Run configured read-only usage_probe_cmd")
    p_u_probe.add_argument("--agent", default=None, help="Target agent (codex|claude|agy)")
    _add_common_flags(p_u_probe)

    p_u_clear = usage_sub.add_parser("clear", help="Clear stored quota state for an agent or all agents")
    p_u_clear.add_argument("--agent", default=None, help="Target agent (codex|claude|agy)")
    _add_common_flags(p_u_clear)

    # candidates
    p_cands = subparsers.add_parser("candidates", help="Emit filtered howhard-compatible candidates")
    p_cands.add_argument("--host", default=None, help="Current host agent (codex|claude|agy)")
    p_cands.add_argument("--include-agents", default=None, help="Comma-separated subset of agents to consider")
    p_cands.add_argument("--output", default=None, help="Write the howhard candidate JSON array to this file")
    _add_common_flags(p_cands)

    # prices
    p_prices = subparsers.add_parser("prices", help="Manage calling-agent resolved model price entries")
    _add_common_flags(p_prices)
    prices_sub = p_prices.add_subparsers(dest="prices_command")

    p_p_pending = prices_sub.add_parser(
        "pending", help="Show discovered models awaiting calling-agent price resolution"
    )
    p_p_pending.add_argument("--host", default=None, help="Current host agent (codex|claude|agy)")
    p_p_pending.add_argument("--include-agents", default=None, help="Comma-separated subset of agents to consider")
    _add_common_flags(p_p_pending)

    p_p_resolve = prices_sub.add_parser(
        "resolve", help="Validate and store calling-agent model price resolutions in <state_root>/prices.json"
    )
    p_p_resolve.add_argument("--file", required=True, help="Path to JSON resolution payload")
    _add_common_flags(p_p_resolve)

    p_p_show = prices_sub.add_parser(
        "show", help="Inspect stored calling-agent model price entries in <state_root>/prices.json"
    )
    _add_common_flags(p_p_show)

    # inventory
    p_inv = subparsers.add_parser("inventory", help="Emit howhard.toml [inventories] block for howhard 2.1+")
    p_inv.add_argument("--host", default=None, help="Current host agent (codex|claude|agy)")
    p_inv.add_argument("--name", default="router", help="Inventory name in howhard.toml (default: router)")
    p_inv.add_argument("--include-agents", default=None, help="Comma-separated subset of agents to consider")
    p_inv.add_argument("--format", choices=("toml", "json"), default="toml", help="Output format")
    p_inv.add_argument("--output", default=None, help="Write inventory output to this file")
    _add_common_flags(p_inv)

    # plan
    p_plan = subparsers.add_parser(
        "plan", help="Resolve a howhard decision JSON into a native or CLI execution plan"
    )
    p_plan.add_argument("--host", default=None, help="Current host agent (codex|claude|agy)")
    p_plan.add_argument("--decision", required=True, help="Path to howhard decision JSON file")
    p_plan.add_argument("--prompt", default=None, help="Authorized task prompt text")
    p_plan.add_argument("--prompt-file", default=None, help="File containing authorized task prompt text")
    p_plan.add_argument("--role", default="Task Worker", help="Worker role/description for native subagents")
    p_plan.add_argument("--subagent-type", default=None, help="Host-specific subagent type override")
    p_plan.add_argument("--workspace", default="inherit", help="Antigravity subagent workspace mode")
    p_plan.add_argument("--include-agents", default=None, help="Comma-separated subset of agents to consider")
    p_plan.add_argument("--output", default=None, help="Write resolved execution plan JSON to this file")
    _add_common_flags(p_plan)

    # models
    p_models = subparsers.add_parser(
        "models", help="Discover or inspect available native and CLI model candidates per agent"
    )
    p_models.add_argument("--refresh", action="store_true", help="Force re-probing live CLI model catalogs")
    _add_common_flags(p_models)

    # list
    p_list = subparsers.add_parser("list", help="Show active and observed model routes")
    p_list.add_argument("--repo", default=None, help="Filter by repository name or path")
    p_list.add_argument("--agent", default=None, help="Filter by target agent (codex|claude|agy)")
    p_list.add_argument("--state", default=None, help="Filter by state (routed|review|clean|loud|silent)")
    p_list.add_argument("--limit", type=int, default=50, help="Maximum routes to display (default: 50)")
    p_list.add_argument("--current-repo", action="store_true", help="Inspect only the current repository")
    _add_common_flags(p_list)

    # show
    p_show = subparsers.add_parser("show", help="Show one model route or decision in detail")
    p_show.add_argument("identifier", help="Route ID, decision ID, or execution ID (or unique prefix)")
    p_show.add_argument("--current-repo", action="store_true", help="Inspect only the current repository")
    _add_common_flags(p_show)

    # tui
    p_tui = subparsers.add_parser("tui", help="Open the realtime model-route terminal view")
    p_tui.add_argument("--refresh-interval", type=float, default=1.0, help="Refresh interval in seconds (default: 1.0)")
    p_tui.add_argument("--repo", default=None, help="Initial repository filter")
    p_tui.add_argument("--agent", default=None, help="Initial target agent filter (codex|claude|agy)")
    p_tui.add_argument("--current-repo", action="store_true", help="Inspect only the current repository")
    _add_common_flags(p_tui)

    return parser


def _parse_include_agents(raw):
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


def _emit(payload, *, as_json=True):
    if isinstance(payload, str) and not as_json:
        sys.stdout.write(payload if payload.endswith("\n") else payload + "\n")
    else:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=False) + "\n")


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help(sys.stderr)
        return 2

    try:
        if args.command == "config" and args.config_command == "set-plan":
            scope = "project" if args.project else "user"
            project_root = None
            if scope == "project" and args.config is None:
                project_root = Router(
                    None,
                    state_directory=args.state_dir,
                    include_user_config=not args.no_user_config,
                ).config.capture_root
            res = set_agent_plan_in_config(
                args.agent,
                args.plan,
                config_path=args.config,
                project=project_root,
                scope=scope,
            )
            _emit(res, as_json=True)
            return 0

        router = Router(
            args.config,
            state_directory=args.state_dir,
            include_user_config=not args.no_user_config,
        )

        if args.command == "config":
            sub = args.config_command or "show"
            if sub == "show":
                _emit(router.config_show(now=args.now), as_json=True)
                return 0

        if args.command == "paths":
            _emit(router.paths(), as_json=True)
            return 0

        if args.command == "usage":
            sub = args.usage_command or "show"
            if sub == "show":
                _emit(router.usage_show(getattr(args, "agent", None), now=args.now), as_json=True)
                return 0
            if sub == "set":
                res = router.usage_set(
                    args.agent,
                    session_used_pct=args.session_used_pct,
                    session_reset_at=args.session_reset_at,
                    weekly_used_pct=args.weekly_used_pct,
                    weekly_reset_at=args.weekly_reset_at,
                    rate_limited=args.rate_limited,
                    cooldown_until=args.cooldown_until,
                    tier=args.tier,
                    tier_used_pct=args.tier_used_pct,
                    tier_reset_at=args.tier_reset_at,
                    now=args.now,
                )
                _emit(res, as_json=True)
                return 0
            if sub == "ingest":
                if args.file and args.text:
                    raise AgRouteError("usage ingest: specify either --file or --text, not both")
                if args.file:
                    try:
                        raw_text = Path(args.file).read_text(encoding="utf-8")
                    except OSError as exc:
                        raise AgRouteError(f"usage ingest: unable to read {args.file}: {exc}") from exc
                elif args.text is not None:
                    raw_text = args.text
                else:
                    raw_text = sys.stdin.read()
                res = router.usage_ingest(args.agent, raw_text, now=args.now)
                _emit(res, as_json=True)
                return 0
            if sub == "probe":
                res = router.usage_probe(args.agent, now=args.now)
                _emit(res, as_json=True)
                return 0
            if sub == "clear":
                res = router.usage_clear(args.agent)
                _emit(res, as_json=True)
                return 0

        if args.command == "candidates":
            include = _parse_include_agents(args.include_agents)
            res = router.candidates(args.host, include_agents=include, now=args.now)
            if args.output:
                atomic_write(args.output, json.dumps(res["candidates"], indent=2) + "\n")
            _emit(res if args.json else res["candidates"], as_json=True)
            return 0

        if args.command == "prices":
            if args.prices_command == "pending":
                include = _parse_include_agents(args.include_agents)
                report = router.prices_pending(args.host, include_agents=include, now=args.now)
                _emit(report if args.json else format_pending_report(report), as_json=args.json)
                return 0
            if args.prices_command == "resolve":
                try:
                    payload = json.loads(Path(args.file).read_text(encoding="utf-8"))
                except (OSError, ValueError, UnicodeError) as exc:
                    raise AgRouteError(f"prices resolve: invalid JSON file {args.file}: {exc}") from exc
                report = router.prices_resolve(payload, now=args.now)
                _emit(report if args.json else format_resolve_report(report), as_json=args.json)
                return 0
            if args.prices_command == "show":
                report = router.prices_show(now=args.now)
                _emit(report if args.json else format_show_report(report), as_json=args.json)
                return 0
            raise AgRouteError("prices: expected one of 'pending', 'resolve', or 'show'")

        if args.command == "inventory":
            include = _parse_include_agents(args.include_agents)
            if args.format == "toml" and not args.json:
                rendered = router.format_inventory_toml(
                    args.host, name=args.name, include_agents=include, now=args.now
                )
                if args.output:
                    atomic_write(args.output, rendered)
                _emit(rendered, as_json=False)
            else:
                res = router.inventory(
                    args.host, name=args.name, include_agents=include, now=args.now
                )
                if args.output:
                    atomic_write(args.output, json.dumps(res, indent=2) + "\n")
                _emit(res, as_json=True)
            return 0

        if args.command == "plan":
            if args.prompt is not None and args.prompt_file is not None:
                raise AgRouteError("plan: specify either --prompt or --prompt-file, not both")
            prompt = args.prompt
            if args.prompt_file is not None:
                try:
                    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
                except OSError as exc:
                    raise AgRouteError(f"plan: unable to read {args.prompt_file}: {exc}") from exc
            try:
                decision_obj = json.loads(Path(args.decision).read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError) as exc:
                raise AgRouteError(f"plan: invalid decision JSON file {args.decision}: {exc}") from exc
            include = _parse_include_agents(args.include_agents)
            plan_obj = router.plan(
                args.host,
                decision_obj,
                prompt=prompt,
                role=args.role,
                subagent_type=args.subagent_type,
                workspace=args.workspace,
                include_agents=include,
                now=args.now,
            )
            if args.output:
                atomic_write(args.output, json.dumps(plan_obj, indent=2) + "\n")
            _emit(plan_obj, as_json=True)
            return 0

        if args.command == "models":
            _emit(router.models(refresh=args.refresh, now=args.now), as_json=True)
            return 0

        if args.command == "list":
            res = router.list_routes(
                all_repos=not args.current_repo,
                repo=args.repo,
                agent=args.agent,
                state=args.state,
                limit=args.limit,
                now=args.now,
            )
            if args.json:
                _emit(res, as_json=True)
            else:
                routes = res["routes"]
                if not routes:
                    _emit("no model routes recorded", as_json=False)
                else:
                    lines = [
                        f"{'STATE':<7} {'MODE':<6} {'REPO':<12} {'AGENT':<11} {'MODEL':<22} {'EFFORT':<7} {'ROUTE':<15} LABEL"
                    ]
                    for r in routes:
                        repo_lbl = tui._repository_label(r["repository"])[:12]
                        ag_lbl = tui._agent_label(r)[:11]
                        lines.append(
                            f"{r['status']:<7} {r['execution_mode']:<6} {repo_lbl:<12} {ag_lbl:<11} "
                            f"{r['model'][:22]:<22} {r['effort'][:7]:<7} {r['route_id']:<15} {r['label'][:48]}"
                        )
                    _emit("\n".join(lines), as_json=False)
            return 0

        if args.command == "show":
            route = router.show_route(
                args.identifier,
                all_repos=not args.current_repo,
                now=args.now,
            )
            if args.json:
                _emit(route, as_json=True)
            else:
                _emit(tui._modal_detail(route), as_json=False)
            return 0

        if args.command == "tui":
            return tui.run(
                lambda: Router(
                    args.config,
                    state_directory=args.state_dir,
                    include_user_config=not args.no_user_config,
                ).snapshot(all_repos=not args.current_repo, now=args.now),
                refresh_interval=args.refresh_interval,
                initial_repository_filter=args.repo,
                initial_agent_filter=args.agent,
            )

    except AgRouteError as exc:
        if getattr(args, "json", False) and hasattr(exc, "details"):
            _emit({**exc.details, "error": str(exc)}, as_json=True)
        print(f"agroute: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

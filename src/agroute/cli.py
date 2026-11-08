"""Command-line interface for agroute."""

import argparse
import json
from pathlib import Path
import sys

from . import __version__
from .files import atomic_write
from .router import Router
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
        prog="agroute",
        description="Configurable local agent router across Codex, Claude Code, and Antigravity.",
    )
    parser.add_argument("--version", action="version", version=f"agroute {__version__}")
    _add_common_flags(parser)

    subparsers = parser.add_subparsers(dest="command")

    # paths
    p_paths = subparsers.add_parser("paths", help="Show resolved capture and state roots")
    _add_common_flags(p_paths)

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
        router = Router(
            args.config,
            state_directory=args.state_dir,
            include_user_config=not args.no_user_config,
        )

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
            _emit(res, as_json=True)
            return 0

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

    except AgRouteError as exc:
        print(f"agroute: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""A credential-free realtime terminal view of all AGRoute model routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import shlex
import threading
import time
from typing import Any, Callable

from rich.text import Text

from . import frame
from .routes import OBSERVED_STATUSES
from .validation import AGENTS, AgRouteError

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical, VerticalScroll
    from textual.events import Resize
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Footer, Input, Label, OptionList, Static
    from textual.widgets.option_list import Option
except ImportError:  # pragma: no cover - the package declares Textual; CLI guard is tested
    App = None


_MISSING = (
    "the route TUI needs Textual — install agroute\n"
    "(the scriptable view is: agr list)"
)

_COMPACT_TABLE_WIDTH = 80
_COMPACT_MODEL_WIDTH = 10
_COMPACT_LABEL_WIDTH = 4
_WIDE_MODEL_WIDTH = 24


def _flexible_widths(viewport_width: int) -> tuple[int, int]:
    """Grow MODEL to a useful cap, then give LABEL the remaining width."""
    extra = max(0, viewport_width - _COMPACT_TABLE_WIDTH)
    model_extra = min(extra, _WIDE_MODEL_WIDTH - _COMPACT_MODEL_WIDTH)
    return (
        _COMPACT_MODEL_WIDTH + model_extra,
        _COMPACT_LABEL_WIDTH + extra - model_extra,
    )


def _fitted_label_width(table: DataTable, row_count: int) -> int:
    """Fit LABEL around a required vertical scrollbar at supported widths."""
    _, preferred = _flexible_widths(table.size.width)
    header_height = table.header_height if table.show_header else 0
    needs_vertical_scrollbar = row_count + header_height > table.size.height
    if table.size.width < _COMPACT_TABLE_WIDTH or not needs_vertical_scrollbar:
        return preferred

    columns = table.ordered_columns
    label_index = 7
    fixed_render_width = sum(
        column.width
        for index, column in enumerate(columns)
        if index != label_index
    ) + 2 * table.cell_padding * len(columns)
    available_width = table.size.width - table.scrollbar_size_vertical
    return max(1, min(preferred, available_width - fixed_render_width))


async def _off_loop(operation: Callable[[], object]) -> object:
    """Run bounded router I/O without owning the event loop's default executor."""
    loop = asyncio.get_running_loop()
    future: asyncio.Future[object] = loop.create_future()

    def deliver(value: object = None, error: BaseException | None = None) -> None:
        if future.done():
            return
        if error is None:
            future.set_result(value)
        else:
            future.set_exception(error)

    def invoke() -> None:
        try:
            value = operation()
        except BaseException as exc:
            try:
                loop.call_soon_threadsafe(deliver, None, exc)
            except RuntimeError:
                pass
        else:
            try:
                loop.call_soon_threadsafe(deliver, value, None)
            except RuntimeError:
                pass

    threading.Thread(target=invoke, name="agroute-tui-io", daemon=True).start()
    return await future


def _moment(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _elapsed(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "—"
    seconds = max(0, round((end - start).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _age(row: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return _elapsed(_moment(row.get("created_at")), now)


_ROUTE_STYLES = {
    "routed": "bold cyan",
    "review": "bold yellow",
    "done": "bold green",
    "done*": "bold yellow",
    "switched": "bold dark_orange",
    "cancelled": "dim",
}

_OUTCOME_STYLES = {
    "clean": "bold green",
    "loud": "bold red",
    "silent": "bold magenta",
    "interrupted": "bold dark_orange",
    "unscored": "dim",
}


def _row_route_status(row: dict) -> str | None:
    if "route_status" in row:
        val = row.get("route_status")
        return str(val) if val else None
    if row.get("execution_mode") == "raw":
        return None
    st = str(row.get("status") or "routed")
    if st in OBSERVED_STATUSES:
        return "done"
    return st


def _row_route_label(row: dict) -> str:
    rs = _row_route_status(row)
    if rs is None:
        return "—"
    if rs == "done" and (row.get("needs_review") or row.get("cap_met") is False):
        return "done*"
    return rs


def _row_outcome_label(row: dict) -> str:
    out = row.get("outcome")
    if out:
        return str(out)
    st = row.get("status")
    if st in OBSERVED_STATUSES:
        return str(st)
    return "—"


def _is_active_row(row: dict) -> bool:
    rs = _row_route_status(row)
    return rs in ("routed", "review")


def _row_matches_state(row: dict, needle: str) -> bool:
    rs = _row_route_status(row)
    out = _row_outcome_label(row)
    return needle in (
        rs,
        _row_route_label(row),
        row.get("outcome"),
        None if out == "—" else out,
        row.get("status"),
    )


class _KeyedText(Text):
    route_id: str = ""


def _compact_cell(value: object, style: str | None = None, *, route_id: str = "") -> Text:
    """Render one fixed-width table value with an explicit overflow marker."""
    cell = _KeyedText(str(value), style=style, no_wrap=True, overflow="ellipsis")
    cell.route_id = route_id
    return cell


def _cell_equal(a: object, b: object) -> bool:
    """Compare table cells by both content and formatting."""
    if isinstance(a, Text) and isinstance(b, Text):
        return (
            a.plain == b.plain
            and a.style == b.style
            and a._spans == b._spans
            and getattr(a, "route_id", "") == getattr(b, "route_id", "")
        )
    return a == b


def _repository_label(repository: object) -> str:
    identity = str(repository or "").strip().rstrip("/")
    if identity.endswith("/.git"):
        identity = identity[:-5].rstrip("/")
    elif identity.endswith(".git"):
        identity = identity[:-4]
    return identity.rsplit("/", 1)[-1] or str(repository or "—")


def _agent_label(row: dict) -> str:
    host = str(row.get("host_agent") or "")
    target = str(row.get("target_agent") or "")
    if host and target and host != target:
        return f"{host}→{target}"
    return target or host or "—"


def _effort_label(effort: object) -> str:
    val = str(effort or "—")
    return "std" if val == "standard" else val


def _invocation_summary(invocation: dict | None) -> str:
    if not isinstance(invocation, dict):
        return "howhard direct decision"
    kind = invocation.get("kind")
    if kind == "native_tool":
        tool_name = invocation.get("tool_name", "native")
        params = invocation.get("parameters") or {}
        compact = {k: v for k, v in params.items() if k not in ("Prompt", "prompt")}
        return f"native_tool {tool_name} {json.dumps(compact, sort_keys=True)}"
    if kind == "cli_command":
        argv = invocation.get("argv") or []
        shown = list(argv[:10])
        if len(argv) > 10:
            shown.append("…")
        return f"cli_command {shlex.join(str(a) for a in shown)}"
    return json.dumps(invocation, sort_keys=True)


def _detail_text(row: dict) -> str:
    cap = "yes" if row.get("cap_met") is True else ("no" if row.get("cap_met") is False else "—")
    review = " · needs_review" if row.get("needs_review") else ""
    affinity = " · host_affinity" if row.get("host_affinity_applied") else ""
    risk = row.get("risk_class") or "—"
    route_lbl = _row_route_label(row)
    outcome_raw = _row_outcome_label(row)
    outcome = "pending" if outcome_raw == "—" else outcome_raw
    cause = row.get("cause")
    outcome_text = f"{outcome} ({cause})" if cause else outcome
    if row.get("outcome_source") == "linked_raw":
        outcome_text += " [linked --native]"
    exec_id = row.get("execution_id") or "—"
    tokens = f"{row['tokens']:,}" if isinstance(row.get("tokens"), int) else "—"
    cost = f"${row['cost_usd']:.4f}" if type(row.get("cost_usd")) in (int, float) else "—"
    session_cost = f"${row['session_cost_usd']:.4f}" if type(row.get("session_cost_usd")) in (int, float) else "—"
    needed_credit = f"${row['needed_credit_usd']:.4f} credit" if type(row.get("needed_credit_usd")) in (int, float) else "—"
    budget = row.get("credit_budget") or {}
    available_credit = (f"${budget['available_credit_usd']:.4f} credit"
                        if type(budget.get("available_credit_usd")) in (int, float) else "—")
    dur = f"{row['seconds']:.1f}s" if type(row.get("seconds")) in (int, float) else "—"
    return (
        f"{row['route_id']} · route={route_lbl} · {row['execution_mode']} "
        f"({row['host_agent']} → {row['target_agent']}) · {_repository_label(row['repository'])}\n"
        f"target: {row['qualified_model']} ({row['effort']}) · type: {row['task_type']} · "
        f"risk: {risk} · cap_met: {cap}{review}{affinity}\n"
        f"invocation: {_invocation_summary(row.get('invocation'))}\n"
        f"selection: session_cost_usd={session_cost} · needed_credit_usd={needed_credit} · available_credit_usd={available_credit}\n"
        f"outcome: {outcome_text} · exec_id: {exec_id} · tokens: {tokens} · duration: {dur} · observed cost: {cost}\n"
        f"label: {row['label']}\n"
        f"routed: {row['created_at']} · observed: {row.get('observed_at') or '—'}"
    )


def _modal_detail(row: dict) -> str:
    route_lbl = _row_route_label(row)
    outcome_raw = _row_outcome_label(row)
    outcome_disp = "pending" if outcome_raw == "—" else outcome_raw
    lines = [
        f"Route ID: {row['route_id']}",
        f"Decision ID: {row.get('decision_id') or '—'}",
        f"Route status (agroute): {route_lbl}",
        f"Status: {row['status']}",
        f"Execution mode: {row['execution_mode']}",
        f"Host agent: {row['host_agent']}",
        f"Target agent: {row['target_agent']}",
        f"Qualified model: {row['qualified_model']}",
        f"Bare model: {row['model']}",
        f"Effort: {row['effort']}",
        f"Task type: {row['task_type']}",
        f"Risk class: {row.get('risk_class') or '—'}",
        f"Cap met: {row.get('cap_met') if row.get('cap_met') is not None else '—'}",
        f"Needs review: {'yes' if row.get('needs_review') else 'no'}",
        f"Host affinity applied: {'yes' if row.get('host_affinity_applied') else 'no'}",
        f"Repository: {row['repository']} ({row['repository_id']})",
        f"Label: {row['label']}",
        f"Routed at: {row['created_at']}",
        f"Observed at: {row.get('observed_at') or '—'}",
        f"Outcome: {outcome_disp}",
        f"Outcome source: {row.get('outcome_source') or '—'}",
        f"Cause: {row.get('cause') or '—'}",
        f"Execution ID: {row.get('execution_id') or '—'}",
        f"Switched from: {row.get('switched_from') or '—'}",
        f"Superseded by: {row.get('superseded_by') or '—'}",
        f"Tokens: {row['tokens'] if row.get('tokens') is not None else '—'}",
        f"Seconds: {row['seconds'] if row.get('seconds') is not None else '—'}",
        f"Observed cost USD: {row['cost_usd'] if row.get('cost_usd') is not None else '—'}",
        f"Closed credit USD: {row.get('credit_usd', '—')}",
        f"Session cost USD: {row.get('session_cost_usd', '—')}",
        f"Needed credit USD: {row.get('needed_credit_usd', '—')}",
        f"Available credit USD: {(row.get('credit_budget') or {}).get('available_credit_usd', '—')}",
        f"Cost per credit: {row.get('cost_per_credit', '—')}",
        f"Monthly subscription cost USD: {row.get('monthly_cost_usd', '—')}",
        f"Weekly subscription cost USD: {row.get('weekly_cost_usd', '—')}",
        f"Invocation: {json.dumps(row['invocation'], sort_keys=True) if row.get('invocation') else '—'}",
    ]
    forecasts = row.get("forecasts") or []
    if forecasts:
        lines.append("\nForecast panel (top 8):")
        for item in forecasts[:8]:
            model = item.get("model") or item.get("family") or "?"
            eff = item.get("effort") or "?"
            clean = item.get("clean")
            loud = item.get("loud")
            silent = item.get("silent")
            if not _is_num(clean) and isinstance(item.get("P_clean"), dict):
                clean = item["P_clean"].get("mean")
                loud = (item.get("P_loud") or {}).get("mean") if isinstance(item.get("P_loud"), dict) else None
                silent = (item.get("P_silent") or {}).get("mean") if isinstance(item.get("P_silent"), dict) else None
            if _is_num(clean):
                extras = []
                if _is_num(item.get("J")):
                    extras.append(f"J=${float(item['J']):.2f}")
                if _is_num(item.get("needed_credit_usd")):
                    extras.append(f"credit=${float(item['needed_credit_usd']):.2f}")
                suffix = f" ({', '.join(extras)})" if extras else ""
                lines.append(
                    f"  {model} ({eff}): clean={float(clean):.3f} loud={float(loud or 0):.3f} silent={float(silent or 0):.3f}{suffix}"
                )
            else:
                lines.append(f"  {json.dumps(item, sort_keys=True)}")
    if row.get("note"):
        lines.append(f"\nObservation note:\n{row['note']}")
    if row.get("task_text"):
        lines.append(f"\nTask prompt excerpt:\n{row['task_text']}")
    return "\n".join(lines)


def _is_num(value: object) -> bool:
    return type(value) in (int, float)


def _fmt_credit(amount: float, *, compact: bool = False) -> str:
    value = float(amount)
    if not compact:
        return f"${value:.2f}"
    if abs(value) < 0.005:
        return "$0"
    if value >= 1000.0:
        return f"${value:,.0f}"
    if value >= 100.0:
        return f"${value:.0f}"
    if abs(value - round(value)) < 0.005:
        return f"${round(value):.0f}"
    return f"${value:.2f}"


def _fmt_pct_val(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{round(value):.0f}%"
    return f"{value:.1f}%"


def _reset_countdown(reset_at: str | None, now: datetime) -> str:
    reset_dt = _moment(reset_at)
    if reset_dt is None:
        return "—"
    seconds = int((reset_dt - now).total_seconds())
    if seconds <= 0:
        return "now"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600:02d}h"


def _pool_window_state(name: str, info: dict, pool_name: str, win_key: str) -> dict:
    if pool_name == "shared":
        return info.get(win_key) or {}
    tiers = info.get("tiers") or {}
    if win_key == "session_5h":
        win = tiers.get(f"{pool_name}-5h")
        if isinstance(win, dict):
            return win
        if name == "agy" and pool_name == "gemini":
            return info.get("session_5h") or {}
        return {}
    for candidate in (f"{pool_name}-weekly", pool_name):
        win = tiers.get(candidate)
        if isinstance(win, dict):
            return win
    if name == "agy" and pool_name == "gemini":
        return info.get("weekly_7d") or {}
    return {}


def _ordered_pool_entries(name: str, cal: dict | None) -> list[tuple[str, dict | None]]:
    if not isinstance(cal, dict) or not cal:
        return [("shared", None)]
    preferred = ["shared", "gemini", "3p"] if name == "agy" else ["shared"]
    ordered_keys: list[str] = []
    for key in preferred:
        if key in cal and isinstance(cal[key], dict):
            ordered_keys.append(key)
    for key, val in cal.items():
        if key not in ordered_keys and isinstance(val, dict):
            ordered_keys.append(key)
    if not ordered_keys:
        return [("shared", None)]
    return [(key, cal[key]) for key in ordered_keys]


def _calibration_for_view(info: dict, *, is_host: bool) -> dict | None:
    if is_host and isinstance(info.get("host_credit_calibration"), dict) and info["host_credit_calibration"]:
        return info["host_credit_calibration"]
    cal = info.get("credit_calibration")
    if not isinstance(cal, dict) or not cal:
        return None
    if not is_host:
        return cal
    limits = info.get("quota_limits") or {}
    host_rsv_pct = float(limits.get("host_reserve_pct", 0.0))
    host_rsv_usd = float(limits.get("host_reserve_credit_usd", 0.0))
    if host_rsv_pct <= 0.0 and host_rsv_usd <= 0.0:
        return cal
    min_rem_usd = float(limits.get("min_remaining_credit_usd", 0.0))
    adjusted: dict[str, dict] = {}
    for pool_name, pool_meta in cal.items():
        if not isinstance(pool_meta, dict):
            continue
        p_copy = dict(pool_meta)
        rem_vals: list[float] = []
        for win_key in ("session_5h", "weekly_7d"):
            w_meta = pool_meta.get(win_key)
            if not isinstance(w_meta, dict):
                continue
            w_copy = dict(w_meta)
            ceiling = w_meta.get("calibrated_credit_usd")
            used_pct = w_meta.get("used_pct")
            reserved_usd = float(w_meta.get("reserved_credit_usd") or 0.0)
            if _is_num(ceiling):
                held_back = max(float(ceiling) * host_rsv_pct / 100.0, min_rem_usd + host_rsv_usd)
                w_copy["reserve_credit_usd"] = round(held_back, 6)
                if _is_num(used_pct):
                    rem = max(0.0, (100.0 - float(used_pct)) / 100.0 * float(ceiling) - held_back - reserved_usd)
                    w_copy["remaining_credit_usd"] = round(rem, 6)
                    rem_vals.append(w_copy["remaining_credit_usd"])
            p_copy[win_key] = w_copy
        if rem_vals:
            p_copy["available_credit_usd"] = min(rem_vals)
        adjusted[pool_name] = p_copy
    return adjusted


def _quota_ledger_text(snapshot: dict, *, host_view: str | None, now: datetime) -> str:
    header_groups = (
        f"{'':14}  {'':9}  {'':8}  {'':3}  "
        f"{'── PCT QUOTA ──':15}   {'───────── CREDIT QUOTA ──────────':33}"
    )
    header_cols = (
        f"{'POOL':<14}  {'PLAN':<9}  {'STATUS':<8}  {'WIN':<3}  "
        f"{'USED':>4} {'RSV':>4} {'LEFT':>5}   "
        f"{'CAP':<7} {'USED':<7} {'RSV':<9} {'LEFT':<7}  RESETS"
    )
    rows_out = [header_groups, header_cols]
    agents = snapshot.get("agents") or {}

    for name in AGENTS:
        info = agents.get(name) or {}
        is_host = host_view is not None and name == host_view
        limits = info.get("quota_limits") or {}
        max_session = float(limits.get("max_session_used_pct", 90.0))
        max_weekly = float(limits.get("max_weekly_used_pct", 95.0))
        max_tier = float(limits.get("max_tier_used_pct", 95.0))
        host_rsv_pct = float(limits.get("host_reserve_pct", 0.0)) if is_host else 0.0
        excluded_groups = set(limits.get("exclude_quota_groups") or ())

        cal = _calibration_for_view(info, is_host=is_host)
        pool_entries = _ordered_pool_entries(name, cal)
        multi_named = len(pool_entries) > 1 or (pool_entries and pool_entries[0][0] != "shared")

        for pool_name, pool_meta in pool_entries:
            base_label = f"{name}/{pool_name}" if multi_named else name
            pool_label = f"{base_label}*" if is_host else base_label
            raw_plan = (
                info.get("effective_plan")
                or (pool_meta.get("plan") if isinstance(pool_meta, dict) else None)
                or info.get("detected_plan")
                or info.get("plan")
            )
            plan_label = str(raw_plan) if raw_plan and str(raw_plan) not in ("auto", "none") else "—"

            win_specs: list[tuple[str, str]] = []
            if isinstance(pool_meta, dict):
                for w_short, w_key in (("5h", "session_5h"), ("7d", "weekly_7d")):
                    w_cal = pool_meta.get(w_key)
                    if not isinstance(w_cal, dict):
                        continue
                    if pool_name != "shared" and w_cal.get("pool_group") == "shared":
                        continue
                    win_specs.append((w_short, w_key))
            if not win_specs:
                win_specs = [("5h", "session_5h"), ("7d", "weekly_7d")]

            # Determine simple per-pool status (OFF / EXCLUDED / BLOCKED / OK).
            pool_blocked = False
            if not info.get("enabled", True):
                status_label = "OFF"
            elif pool_name in excluded_groups:
                status_label = "EXCLUDED"
            else:
                if info.get("rate_limited") or info.get("cooldown_until"):
                    pool_blocked = True
                if isinstance(pool_meta, dict) and _is_num(pool_meta.get("available_credit_usd")):
                    if float(pool_meta["available_credit_usd"]) <= 0.0:
                        pool_blocked = True
                for _ws, wk in win_specs:
                    w_state = _pool_window_state(name, info, pool_name, wk)
                    w_cal = pool_meta.get(wk) if isinstance(pool_meta, dict) else None
                    u_pct = w_cal.get("used_pct") if isinstance(w_cal, dict) and "used_pct" in w_cal else w_state.get("used_pct")
                    base_lim = max(0.0, (max_session if wk == "session_5h" else max_weekly) - host_rsv_pct)
                    eff_lim = base_lim if pool_name == "shared" else min(max_tier, base_lim)
                    if w_state.get("status") == "rejected":
                        pool_blocked = True
                    elif _is_num(u_pct) and float(u_pct) >= eff_lim:
                        pool_blocked = True
                models_map = info.get("models") or {}
                pool_models = [m for m in models_map.values() if isinstance(m, dict) and m.get("quota_group") == pool_name]
                if pool_models:
                    elig_key = "native_eligible" if is_host else "eligible"
                    if not any(m.get(elig_key, m.get("eligible", True)) for m in pool_models):
                        pool_blocked = True
                elif len(pool_entries) == 1:
                    agent_elig_key = "eligible_as_host_native" if is_host else "eligible_as_target"
                    if not info.get(agent_elig_key, True):
                        pool_blocked = True
                status_label = "BLOCKED" if pool_blocked else "OK"

            for idx, (w_short, w_key) in enumerate(win_specs):
                w_state = _pool_window_state(name, info, pool_name, w_key)
                w_cal = pool_meta.get(w_key) if isinstance(pool_meta, dict) else None
                u_pct = (
                    w_cal.get("used_pct")
                    if isinstance(w_cal, dict) and "used_pct" in w_cal
                    else w_state.get("used_pct")
                )
                base_lim = max(0.0, (max_session if w_key == "session_5h" else max_weekly) - host_rsv_pct)
                eff_lim = base_lim if pool_name == "shared" else min(max_tier, base_lim)
                pct_rsv = max(0.0, 100.0 - eff_lim)

                p_used_str = _fmt_pct_val(float(u_pct)) if _is_num(u_pct) else "—"
                p_rsv_str = _fmt_pct_val(pct_rsv)
                p_left_str = _fmt_pct_val(max(0.0, eff_lim - float(u_pct))) if _is_num(u_pct) else "—"

                ceiling = w_cal.get("calibrated_credit_usd") if isinstance(w_cal, dict) else None
                hold_rsv = w_cal.get("reserve_credit_usd") if isinstance(w_cal, dict) else None
                live_rsv = w_cal.get("reserved_credit_usd") if isinstance(w_cal, dict) else None
                rem_usd = w_cal.get("remaining_credit_usd") if isinstance(w_cal, dict) else None

                c_cap_str = _fmt_credit(float(ceiling), compact=True) if _is_num(ceiling) else "—"
                c_used_str = (
                    _fmt_credit(max(0.0, float(u_pct) / 100.0 * float(ceiling)), compact=True)
                    if (_is_num(ceiling) and _is_num(u_pct))
                    else "—"
                )
                if _is_num(hold_rsv):
                    if _is_num(live_rsv) and float(live_rsv) > 0.0:
                        c_rsv_str = f"{_fmt_credit(float(hold_rsv), compact=True)}+{_fmt_credit(float(live_rsv), compact=True)}"
                    else:
                        c_rsv_str = _fmt_credit(float(hold_rsv), compact=True)
                else:
                    c_rsv_str = "—"
                c_left_str = _fmt_credit(float(rem_usd), compact=True) if _is_num(rem_usd) else "—"
                reset_str = _reset_countdown(w_state.get("reset_at"), now)

                left_prefix = (
                    f"{pool_label:<14}  {plan_label:<9}  {status_label:<8}"
                    if idx == 0
                    else f"{'':14}  {'':9}  {'':8}"
                )
                rows_out.append(
                    f"{left_prefix}  {w_short:<3}  "
                    f"{p_used_str:>4} {p_rsv_str:>4} {p_left_str:>5}   "
                    f"{c_cap_str:<7} {c_used_str:<7} {c_rsv_str:<9} {c_left_str:<7}  {reset_str}"
                )

    return "\n".join(rows_out)


def _primary_credit_pool(name: str, info: dict) -> dict | None:
    cal = info.get("credit_calibration")
    if not isinstance(cal, dict) or not cal:
        return None
    if isinstance(cal.get("shared"), dict):
        return cal["shared"]
    if name == "agy" and isinstance(cal.get("gemini"), dict):
        return cal["gemini"]
    if len(cal) == 1:
        sole = next(iter(cal.values()))
        return sole if isinstance(sole, dict) else None
    return None


def _window_credit_detail(win_cal: dict | None, used_pct: object) -> str:
    if not isinstance(win_cal, dict):
        return ""
    ceiling = win_cal.get("calibrated_credit_usd")
    rem = win_cal.get("remaining_credit_usd")
    reserve = win_cal.get("reserve_credit_usd")
    if not _is_num(ceiling):
        if _is_num(rem):
            return f" (~{_fmt_credit(rem)} left)"
        return ""
    if _is_num(used_pct):
        used_credit = max(0.0, float(used_pct) / 100.0 * float(ceiling))
        parts = [f"~{_fmt_credit(used_credit)}/{_fmt_credit(ceiling)} credit"]
        if _is_num(rem):
            parts.append(f"~{_fmt_credit(rem)} left")
        if _is_num(reserve) and float(reserve) > 0.0:
            parts.append(f"{_fmt_credit(reserve)} reserve")
        return f" ({', '.join(parts)})"
    parts = [f"est. ceiling ~{_fmt_credit(ceiling)} credit"]
    if _is_num(reserve) and float(reserve) > 0.0:
        parts.append(f"{_fmt_credit(reserve)} reserve")
    return f" ({', '.join(parts)})"


def _badge_credit_suffix(name: str, info: dict) -> str:
    primary = _primary_credit_pool(name, info)
    if not isinstance(primary, dict):
        return ""
    s5_rem = (primary.get("session_5h") or {}).get("remaining_credit_usd")
    w7_rem = (primary.get("weekly_7d") or {}).get("remaining_credit_usd")
    if _is_num(s5_rem) and _is_num(w7_rem):
        return f" (~{_fmt_credit(s5_rem, compact=True)}/{_fmt_credit(w7_rem, compact=True)})"
    if _is_num(s5_rem):
        return f" (~{_fmt_credit(s5_rem, compact=True)}/—)"
    if _is_num(w7_rem):
        return f" (—/~{_fmt_credit(w7_rem, compact=True)})"
    avail = primary.get("available_credit_usd")
    if _is_num(avail):
        return f" (~{_fmt_credit(avail, compact=True)})"
    return ""


def _agent_quota_badge(name: str, info: dict) -> str:
    if not info.get("enabled", True):
        return f"{name} off"
    if not info.get("eligible_as_target", True):
        return f"{name} lim"
    s5 = (info.get("session_5h") or {}).get("used_pct")
    w7 = (info.get("weekly_7d") or {}).get("used_pct")
    credit_suffix = _badge_credit_suffix(name, info)
    if _is_num(s5) or _is_num(w7):
        s_txt = f"{s5:.0f}%" if _is_num(s5) else "—"
        w_txt = f"{w7:.0f}%" if _is_num(w7) else "—"
        return f"{name} {s_txt}/{w_txt}{credit_suffix}"
    return f"{name} ok{credit_suffix}"


def _usage_modal_body(snapshot: dict) -> str:
    lines = [f"Evaluated at: {snapshot.get('captured_at', '—')}", ""]
    agents = snapshot.get("agents") or {}
    for name in AGENTS:
        info = agents.get(name) or {}
        s5 = info.get("session_5h") or {}
        w7 = info.get("weekly_7d") or {}
        primary = _primary_credit_pool(name, info)
        s5_cal = primary.get("session_5h") if isinstance(primary, dict) else None
        w7_cal = primary.get("weekly_7d") if isinstance(primary, dict) else None
        lines.append(f"[{name}]")
        lines.append(f"  Enabled: {info.get('enabled', True)}")
        if info.get("monthly_cost_usd") is not None:
            weekly_cost = info.get("weekly_cost_usd")
            lines.append(
                f"  Subscription cost: monthly_cost_usd=${info['monthly_cost_usd']:.2f}, "
                f"weekly_cost_usd=${weekly_cost:.2f}"
                if _is_num(weekly_cost)
                else f"  Subscription cost: monthly_cost_usd=${info['monthly_cost_usd']:.2f}"
            )
        lines.append(
            f"  Eligible as target: {info.get('eligible_as_target', True)}"
            + (f" ({info['target_exclusion_reason']})" if info.get("target_exclusion_reason") else "")
        )
        lines.append(
            f"  Eligible as host native: {info.get('eligible_as_host_native', True)}"
            + (f" ({info['host_native_exclusion_reason']})" if info.get("host_native_exclusion_reason") else "")
        )
        s5_credit = _window_credit_detail(s5_cal, s5.get("used_pct"))
        w7_credit = _window_credit_detail(w7_cal, w7.get("used_pct"))
        lines.append(
            f"  5h session used: {s5.get('used_pct') if s5.get('used_pct') is not None else '—'}%"
            f"{s5_credit} (reset: {s5.get('reset_at') or '—'})"
        )
        lines.append(
            f"  7d weekly used:  {w7.get('used_pct') if w7.get('used_pct') is not None else '—'}%"
            f"{w7_credit} (reset: {w7.get('reset_at') or '—'})"
        )
        lines.append(
            f"  Rate-limited: {info.get('rate_limited', False)} "
            f"(cooldown: {info.get('cooldown_until') or '—'})"
        )
        cal_pools = info.get("credit_calibration") or {}
        if isinstance(cal_pools, dict) and cal_pools:
            plan_label = info.get("effective_plan") or next(
                (p.get("plan") for p in cal_pools.values() if isinstance(p, dict) and p.get("plan")),
                None,
            )
            lines.append(
                f"  Estimated credit pools (plan: {plan_label}):"
                if plan_label
                else "  Estimated credit pools:"
            )
            for pool_name, p_meta in cal_pools.items():
                if not isinstance(p_meta, dict):
                    continue
                parts = []
                avail = p_meta.get("available_credit_usd")
                if _is_num(avail):
                    parts.append(f"available=~{_fmt_credit(avail)} credit")
                for win_key, label in (("session_5h", "5h"), ("weekly_7d", "7d")):
                    w_meta = p_meta.get(win_key)
                    if not isinstance(w_meta, dict):
                        continue
                    w_pct = w_meta.get("used_pct")
                    pct_str = f"{w_pct:.1f}%" if _is_num(w_pct) else "—%"
                    parts.append(f"{label}={pct_str}{_window_credit_detail(w_meta, w_pct)}")
                if parts:
                    lines.append(f"    {pool_name}: {' | '.join(parts)}")
        tiers = info.get("tiers") or {}
        if tiers:
            lines.append(f"  Tiers: {json.dumps(tiers, sort_keys=True)}")
        models = info.get("models") or {}
        if models:
            lines.append("  Per-model quota:")
            for mid, mq in models.items():
                ms5 = (mq.get("session_5h") or {}).get("used_pct")
                mw7 = (mq.get("weekly_7d") or {}).get("used_pct")
                budget = mq.get("credit_budget") or {}
                m_cal = mq.get("credit_calibration") or {}
                ms5_rem = budget.get("session_5h_remaining_credit_usd")
                mw7_rem = budget.get("weekly_7d_remaining_credit_usd")
                if not _is_num(ms5_rem) and isinstance(m_cal.get("session_5h"), dict):
                    ms5_rem = m_cal["session_5h"].get("remaining_credit_usd")
                if not _is_num(mw7_rem) and isinstance(m_cal.get("weekly_7d"), dict):
                    mw7_rem = m_cal["weekly_7d"].get("remaining_credit_usd")
                s_str = f"{ms5:.1f}%" if _is_num(ms5) else "—"
                if _is_num(ms5_rem):
                    s_str += f" (~{_fmt_credit(ms5_rem)} left)"
                w_str = f"{mw7:.1f}%" if _is_num(mw7) else "—"
                if _is_num(mw7_rem):
                    w_str += f" (~{_fmt_credit(mw7_rem)} left)"
                elig_str = "eligible" if mq.get("eligible", True) else f"blocked ({mq.get('exclusion_reason')})"
                cost_per_credit = mq.get("cost_per_credit")
                budget_str = (
                    f" available_credit_usd=${budget['available_credit_usd']:.2f} credit"
                    if _is_num(budget.get("available_credit_usd"))
                    else ""
                )
                lines.append(
                    f"    {name}/{mid} [{mq.get('quota_group', 'shared')}]: 5h={s_str} 7d={w_str} "
                    f"cost_per_credit={cost_per_credit if cost_per_credit is not None else '—'}{budget_str} -> {elig_str}"
                )
        lines.append("")
    return "\n".join(lines).rstrip()


def build_app(
    snapshot_factory: Callable[[], dict],
    *,
    refresh_interval: float = 1.0,
    probe_factory: Callable[[bool], Any] | None = None,
    limit: int | None = 200,
    initial_repository_filter: str | None = None,
    initial_agent_filter: str | None = None,
):
    """Build the standalone TUI app without opening network connections or credentials."""
    if App is None:  # pragma: no cover
        raise RuntimeError(_MISSING)
    if refresh_interval <= 0:
        raise ValueError("refresh_interval must be positive")

    class Show(ModalScreen[None]):
        BINDINGS = [Binding("escape,enter,q", "close", "close")]

        def __init__(self, title: str, body: str, *, wide: bool = False) -> None:
            super().__init__()
            self.title_text = title
            self.body_text = body
            self.wide = wide

        def compose(self) -> ComposeResult:
            with Vertical(id="show", classes="wide" if self.wide else ""):
                yield Label(self.title_text, markup=False, id="show-title")
                with VerticalScroll(id="show-scroll"):
                    yield Static(self.body_text, markup=False, id="show-body")
                yield Static("Enter close", markup=False, id="show-keys", classes="status")

        def on_mount(self) -> None:
            if self.wide:
                self.query_one("#show", Vertical).styles.height = "94%"
                self.query_one("#show-scroll", VerticalScroll).styles.height = "1fr"

        def action_close(self) -> None:
            self.dismiss(None)

    class FilterPicker(ModalScreen[tuple[bool, str | None]]):
        BINDINGS = [Binding("escape", "cancel", "keep current filter")]

        def __init__(
            self,
            title: str,
            all_label: str,
            choices: list[tuple[str, str]],
            current: str | None,
        ) -> None:
            super().__init__()
            self.title_text = title
            self.all_label = all_label
            self.choices = choices
            self.current = current
            self._visible_choices: list[tuple[str | None, str]] = []

        def compose(self) -> ComposeResult:
            with Vertical(id="filter-picker"):
                yield Label(self.title_text, markup=False, id="filter-title")
                yield Input(
                    placeholder="Type to narrow choices",
                    id="filter-query",
                )
                yield OptionList(id="filter-options")
                yield Static(
                    "Type to narrow · Tab choices · Enter apply · Esc keep current",
                    markup=False,
                    id="filter-keys",
                    classes="status",
                )

        def on_mount(self) -> None:
            self._render_options("")
            self.query_one("#filter-query", Input).focus()

        def _render_options(self, query: str) -> None:
            needle = query.strip().casefold()
            matches = [
                (value, label)
                for value, label in self.choices
                if not needle or needle in label.casefold()
            ]
            self._visible_choices = [(None, self.all_label), *matches]
            options = self.query_one("#filter-options", OptionList)
            options.clear_options()
            options.add_options(
                Option(
                    Text(label, no_wrap=True, overflow="ellipsis"),
                    id=f"filter-choice-{index}",
                )
                for index, (_value, label) in enumerate(self._visible_choices)
            )
            if needle:
                options.highlighted = 1 if matches else None
                return
            options.highlighted = next(
                (
                    index
                    for index, (value, _label) in enumerate(self._visible_choices)
                    if value == self.current
                ),
                0,
            )

        def _accept(self, index: int | None) -> None:
            if index is None or not 0 <= index < len(self._visible_choices):
                return
            self.dismiss((True, self._visible_choices[index][0]))

        def on_input_changed(self, event: Input.Changed) -> None:
            self._render_options(event.value)

        def on_input_submitted(self, _event: Input.Submitted) -> None:
            options = self.query_one("#filter-options", OptionList)
            self._accept(options.highlighted)

        def on_option_list_option_selected(
            self, event: OptionList.OptionSelected
        ) -> None:
            self._accept(event.option_index)

        def action_cancel(self) -> None:
            self.dismiss((False, None))

    class RouteTable(DataTable):
        BINDINGS = [
            Binding("enter", "select_cursor", "details"),
            *DataTable.BINDINGS[1:],
        ]
        resize_callback: Callable[[], None] | None = None

        def on_resize(self, _event: Resize) -> None:
            if self.resize_callback is not None:
                self.call_after_refresh(self.resize_callback)

    class RouterApp(App):
        TITLE = "AGRoute"
        CSS = frame.CSS + """
        #quota-strip { height: auto; max-height: 12; padding: 0 1; background: #ffffff; color: #111111; }
        #routes { min-height: 4; height: 1fr; }
        #route-detail { height: 6; }
        #filter-picker { width: 76%; max-width: 96; height: 70%; max-height: 24;
                         padding: 0 1; background: #ffffff; color: #111111;
                         border: double #111111; }
        #filter-title { height: auto; padding: 0 1; background: #ddddda;
                        color: #111111; text-style: bold; }
        #filter-query { margin: 1 1 0 1; }
        #filter-options { height: 1fr; margin: 0 1; background: #ffffff;
                          color: #111111; }
        #filter-keys { height: 1; }
        """
        ENABLE_COMMAND_PALETTE = False
        BINDINGS = [
            Binding("u", "usage", "usage"),
            Binding("H", "cycle_host", "host"),
            Binding("h", "history", "hide/show observed"),
            Binding("p", "repository", "repository filter"),
            Binding("P", "probe", "probe"),
            Binding("a", "agent", "agent filter"),
            Binding("m", "model", "model filter"),
            Binding("s", "state", "state filter"),
            Binding("r", "refresh", "refresh"),
            Binding("q", "quit", "quit"),
            Binding("question_mark", "keys", "keys"),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.theme = "textual-light"
            self._snapshot_factory = snapshot_factory
            self._rows: dict[str, dict] = {}
            self._reading = False
            self._probing = False
            self._refresh_pending = False
            self._last_probe = float("-inf")
            self._message = "loading …"
            self._snapshot: dict | None = None
            self._host_override = False
            self._host_view: str | None = None
            self._show_history = True
            self._repository_filter: str | None = initial_repository_filter
            self._agent_filter: str | None = initial_agent_filter
            self._model_filter: str | None = None
            self._state_filter: str | None = None

        def compose(self) -> ComposeResult:
            with Vertical():
                yield Static(
                    "MACHINE │ AGROUTE │ loading",
                    markup=False,
                    classes="subject",
                    id="route-subject",
                )
                yield Static(
                    "loading quota …",
                    markup=False,
                    id="quota-strip",
                )
                yield Static("", markup=False, classes="detail-rule", id="quota-rule")
                yield RouteTable(id="routes", cursor_type="row")
                yield Static("", markup=False, classes="detail-rule", id="route-rule")
                yield Static(
                    "loading AGRoute …",
                    markup=False,
                    classes="detail",
                    id="route-detail",
                )
                yield Static(
                    "reading routes …",
                    markup=False,
                    classes="status",
                    id="route-status",
                )
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#routes", RouteTable)
            table.resize_callback = self._render_resized_table
            table.cell_padding = 1
            model_width, label_width = _flexible_widths(table.size.width)
            for label, width in (
                ("ROUTE", 8),
                ("OUTCOME", 8),
                ("MODE", 6),
                ("REPO", 8),
                ("AGENT", 8),
                ("MODEL", model_width),
                ("EFFORT", 5),
                ("LABEL", label_width),
                ("AGE", 5),
            ):
                table.add_column(label, width=width)
            self.action_refresh()
            self._start_probe(False)
            self.set_interval(refresh_interval, self.action_refresh)

        def _render_resized_table(self) -> None:
            if self._snapshot is not None:
                self._render_snapshot(self._snapshot)

        def action_refresh(self) -> None:
            self._start_probe(False)
            if self._reading:
                return
            self._reading = True

            async def work() -> None:
                try:
                    result = await _off_loop(self._snapshot_factory)
                except AgRouteError as exc:
                    self._read_failed(exc)
                    return
                self._apply_snapshot(result)  # type: ignore[arg-type]

            self.run_worker(work(), group="route-snapshot")

        def action_probe(self) -> None:
            self._start_probe(True)

        def _start_probe(self, manual: bool) -> None:
            if probe_factory is None or self._probing:
                return
            if not manual and time.monotonic() - self._last_probe < 60:
                return
            self._probing = True
            self._last_probe = time.monotonic()

            async def work() -> None:
                try:
                    await _off_loop(lambda: probe_factory(manual))
                except Exception:
                    # A provider failure must not terminate the interface.
                    pass
                finally:
                    self._probing = False
                    if self.is_running:
                        if self._reading:
                            self._refresh_pending = True
                        else:
                            self.action_refresh()

            self.run_worker(work(), group="usage-probe")

        def _read_failed(self, exc: AgRouteError) -> None:
            if not self.is_running:
                return
            self._reading = False
            self._message = f"unavailable · {exc}"
            self.query_one("#route-subject", Static).update("MACHINE │ AGROUTE │ unavailable")
            if self._snapshot is None:
                self.query_one("#quota-strip", Static).update("quota unavailable")
            self.query_one("#route-status", Static).update(self._message)
            if not self._rows:
                self.query_one("#route-detail", Static).update(
                    "AGRoute unavailable\n" + str(exc)
                )

        def _selected_id(self) -> str | None:
            table = self.query_one("#routes", DataTable)
            if table.row_count == 0 or table.cursor_row is None:
                return None
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
            return str(key.value)

        def _selected(self) -> dict | None:
            selected = self._selected_id()
            return self._rows.get(selected) if selected else None

        def _apply_snapshot(self, snapshot: dict) -> None:
            if not self.is_running:
                return
            self._reading = False
            self._snapshot = snapshot
            self._render_snapshot(snapshot)
            if self._refresh_pending:
                self._refresh_pending = False
                self.action_refresh()

        def _render_snapshot(self, snapshot: dict) -> None:
            selected = self._selected_id()
            table = self.query_one("#routes", DataTable)
            viewport = (table.scroll_x, table.scroll_y)
            all_rows = list(snapshot.get("routes") or [])
            ordered = list(all_rows)
            if not self._show_history:
                ordered = [row for row in ordered if _is_active_row(row)]
            if self._repository_filter is not None:
                ordered = [
                    row
                    for row in ordered
                    if self._repository_filter
                    in (row["repository_id"], row["repository"], _repository_label(row["repository"]))
                ]
            if self._agent_filter is not None:
                ordered = [row for row in ordered if row["target_agent"] == self._agent_filter]
            if self._model_filter is not None:
                ordered = [row for row in ordered if row["model"] == self._model_filter]
            if self._state_filter is not None:
                ordered = [row for row in ordered if _row_matches_state(row, self._state_filter)]

            if limit is not None:
                completed = 0
                visible = []
                for row in ordered:
                    if _is_active_row(row):
                        visible.append(row)
                    elif completed < limit:
                        visible.append(row)
                        completed += 1
                ordered = visible

            self._rows = {row["route_id"]: row for row in ordered}
            model_width, _ = _flexible_widths(table.size.width)
            table.ordered_columns[5].width = model_width
            table.ordered_columns[7].width = _fitted_label_width(table, len(ordered))
            table.virtual_size = table.virtual_size.with_width(
                sum(column.get_render_width(table) for column in table.ordered_columns)
            )
            now = _moment(snapshot.get("captured_at")) or datetime.now(timezone.utc)
            rendered = {}
            for row in ordered:
                rt_lbl = _row_route_label(row)
                out_lbl = _row_outcome_label(row)
                rendered[row["route_id"]] = (
                    _compact_cell(
                        rt_lbl,
                        style=_ROUTE_STYLES.get(rt_lbl),
                        route_id=row["route_id"],
                    ),
                    _compact_cell(
                        out_lbl,
                        style=_OUTCOME_STYLES.get(out_lbl),
                    ),
                    _compact_cell(row["execution_mode"]),
                    _compact_cell(_repository_label(row["repository"])),
                    _compact_cell(_agent_label(row)),
                    _compact_cell(row["model"]),
                    _compact_cell(_effort_label(row["effort"])),
                    _compact_cell(row["label"]),
                    _compact_cell(_age(row, now)),
                )

            existing = [str(row.key.value) for row in table.ordered_rows]
            existing_set = set(existing)
            desired = [row["route_id"] for row in ordered]
            desired_set = set(desired)

            for route_id in desired:
                if route_id not in existing_set:
                    table.add_row(*rendered[route_id], key=route_id)
            for route_id in existing:
                if route_id not in desired_set:
                    table.remove_row(route_id)

            columns = table.ordered_columns
            for route_id in desired:
                current = table.get_row(route_id)
                for column, old, new in zip(columns, current, rendered[route_id]):
                    if not _cell_equal(old, new):
                        table.update_cell(route_id, column.key, new)

            current_order = [str(row.key.value) for row in table.ordered_rows]
            if current_order != desired:
                positions = {route_id: index for index, route_id in enumerate(desired)}
                table.sort(
                    columns[0].key,
                    key=lambda value: positions.get(getattr(value, "route_id", ""), len(positions)),
                )

            if selected in self._rows:
                table.move_cursor(
                    row=desired.index(selected),
                    scroll=False,
                )
            elif ordered:
                table.move_cursor(row=0, scroll=False)

            table.call_after_refresh(
                table.scroll_to,
                x=viewport[0],
                y=viewport[1],
                animate=False,
                force=True,
                immediate=True,
            )

            routed_count = snapshot.get("routed_count", 0)
            observed_count = snapshot.get("observed_count", 0)
            history = "shown · h hide" if self._show_history else "hidden · h show"
            repository = "all repos"
            if self._repository_filter is not None:
                repository = next(
                    (
                        _repository_label(row["repository"])
                        for row in all_rows
                        if self._repository_filter
                        in (row["repository_id"], row["repository"], _repository_label(row["repository"]))
                    ),
                    self._repository_filter,
                )
            agent = self._agent_filter or "all agents"
            model = self._model_filter or "all models"
            state = self._state_filter or "all states"
            effective_host = (
                self._host_view
                if self._host_override
                else (snapshot.get("default_host") or "agy")
            )
            host_label = effective_host or "none"
            self.query_one("#route-subject", Static).update(
                f"MACHINE │ AGROUTE │ {routed_count} routed · {observed_count} observed · "
                f"host: {host_label} · {repository} · {agent} · {model} · {state} · observed {history}"
            )
            self.query_one("#quota-strip", Static).update(
                _quota_ledger_text(snapshot, host_view=effective_host, now=now)
            )
            self._message = (
                f"showing {len(ordered)}/{len(all_rows)} routes · "
                f"read {snapshot.get('captured_at', '—')}"
            )
            self.query_one("#route-status", Static).update(self._message)
            self._update_detail()

        def _update_detail(self) -> None:
            width = max(1, self.size.width - 2)
            rule = "─" * width
            self.query_one("#quota-rule", Static).update(rule)
            self.query_one("#route-rule", Static).update(rule)
            row = self._selected()
            if row is None:
                self.query_one("#route-detail", Static).update(
                    "no matching routes"
                    if self._snapshot is not None
                    else "loading AGRoute …"
                )
                return
            self.query_one("#route-detail", Static).update(_detail_text(row))

        def action_cycle_host(self) -> None:
            cycle: tuple[str | None, ...] = ("agy", "claude", "codex", None)
            current = (
                self._host_view
                if self._host_override
                else ((self._snapshot or {}).get("default_host") or "agy")
            )
            idx = cycle.index(current) if current in cycle else 0
            self._host_override = True
            self._host_view = cycle[(idx + 1) % len(cycle)]
            if self._snapshot is not None:
                self._render_snapshot(self._snapshot)

        def on_resize(self, _event: Resize) -> None:
            self._update_detail()

        def on_data_table_row_highlighted(
            self, _event: DataTable.RowHighlighted
        ) -> None:
            self._update_detail()

        def on_data_table_row_selected(
            self, _event: DataTable.RowSelected
        ) -> None:
            row = self._selected()
            if row is not None:
                self.push_screen(
                    Show(
                        f"{row['qualified_model']} ({row['effort']}) · {row['route_id']}",
                        _modal_detail(row),
                        wide=True,
                    )
                )

        def action_history(self) -> None:
            self._show_history = not self._show_history
            if self._snapshot is not None:
                self._render_snapshot(self._snapshot)

        def action_usage(self) -> None:
            if self._snapshot is None:
                return
            self.push_screen(
                Show(
                    "Agent Usage & Eligibility",
                    _usage_modal_body(self._snapshot),
                    wide=True,
                )
            )

        def _filter_rows(self) -> list[dict]:
            if self._snapshot is None:
                return []
            rows = list(self._snapshot.get("routes") or [])
            if not self._show_history:
                rows = [r for r in rows if _is_active_row(r)]
            return rows

        def action_repository(self) -> None:
            seen: dict[str, str] = {}
            for row in self._filter_rows():
                repo_id = str(row["repository_id"])
                seen.setdefault(
                    repo_id,
                    f"{_repository_label(row['repository'])} · {row['repository']}",
                )
            if self._repository_filter is not None:
                seen.setdefault(self._repository_filter, self._repository_filter)
            choices = sorted(seen.items(), key=lambda item: item[1].lower())
            self.push_screen(
                FilterPicker(
                    "Repository filter",
                    "All repositories",
                    choices,
                    self._repository_filter,
                ),
                self._apply_repository_filter,
            )

        def _apply_repository_filter(
            self, result: tuple[bool, str | None] | None
        ) -> None:
            if not result or not result[0]:
                return
            self._repository_filter = result[1]
            if self._snapshot is not None:
                self._render_snapshot(self._snapshot)

        def action_agent(self) -> None:
            agents = {str(row["target_agent"]) for row in self._filter_rows()}
            agents.update(AGENTS)
            if self._agent_filter is not None:
                agents.add(self._agent_filter)
            choices = [(ag, ag) for ag in sorted(agents)]
            self.push_screen(
                FilterPicker(
                    "Target agent filter",
                    "All agents",
                    choices,
                    self._agent_filter,
                ),
                self._apply_agent_filter,
            )

        def _apply_agent_filter(
            self, result: tuple[bool, str | None] | None
        ) -> None:
            if not result or not result[0]:
                return
            self._agent_filter = result[1]
            if self._snapshot is not None:
                self._render_snapshot(self._snapshot)

        def action_model(self) -> None:
            models = {str(row["model"]) for row in self._filter_rows()}
            if self._model_filter is not None:
                models.add(self._model_filter)
            choices = [(m, m) for m in sorted(models)]
            self.push_screen(
                FilterPicker(
                    "Model filter",
                    "All models",
                    choices,
                    self._model_filter,
                ),
                self._apply_model_filter,
            )

        def _apply_model_filter(
            self, result: tuple[bool, str | None] | None
        ) -> None:
            if not result or not result[0]:
                return
            self._model_filter = result[1]
            if self._snapshot is not None:
                self._render_snapshot(self._snapshot)

        def action_state(self) -> None:
            states: set[str] = set()
            for row in self._filter_rows():
                rs = _row_route_status(row)
                if rs:
                    states.add(rs)
                out = _row_outcome_label(row)
                if out != "—":
                    states.add(out)
            states.update(
                (
                    "routed",
                    "review",
                    "done",
                    "switched",
                    "cancelled",
                    "clean",
                    "loud",
                    "silent",
                    "interrupted",
                    "unscored",
                )
            )
            if self._state_filter is not None:
                states.add(self._state_filter)
            choices = [(st, st) for st in sorted(states)]
            self.push_screen(
                FilterPicker(
                    "Route / outcome filter",
                    "All states",
                    choices,
                    self._state_filter,
                ),
                self._apply_state_filter,
            )

        def _apply_state_filter(
            self, result: tuple[bool, str | None] | None
        ) -> None:
            if not result or not result[0]:
                return
            self._state_filter = result[1]
            if self._snapshot is not None:
                self._render_snapshot(self._snapshot)

        def action_keys(self) -> None:
            lines = [
                f"{binding.key:14} {binding.description}"
                for binding in [
                    Binding("enter", "select_cursor", "show selected route details"),
                    *self.BINDINGS,
                ]
            ]
            self.push_screen(
                Show(
                    "Keys",
                    "\n".join(lines)
                    + "\n\nROUTE (agroute lifecycle):"
                    + "\n  routed      planned route in flight, awaiting completion"
                    + "\n  review      planned route in flight with silent-risk review required"
                    + "\n  done/done*  route finished (done* preserves needs_review / cap_met=false)"
                    + "\n  switched    route handed off mid-task via model switch (agr close --status switched)"
                    + "\n  cancelled   planned route declined or abandoned before execution (agr close --status cancelled)"
                    + "\n\nOUTCOME (howhard observation):"
                    + "\n  clean       observed execution completed without rework or escaped defect"
                    + "\n  loud        observed execution required caught rework/retry"
                    + "\n  silent      observed execution had an escaped defect"
                    + "\n  interrupted observed execution stopped operationally (e.g. usage_limit)"
                    + "\n  unscored    route closed as done in agroute without scoring howhard",
                )
            )

    return RouterApp()


def run(
    snapshot_factory: Callable[[], dict],
    *,
    refresh_interval: float = 1.0,
    probe_factory: Callable[[bool], Any] | None = None,
    limit: int | None = 200,
    initial_repository_filter: str | None = None,
    initial_agent_filter: str | None = None,
) -> int:
    if App is None:  # pragma: no cover
        print(_MISSING)
        return 2
    build_app(
        snapshot_factory,
        refresh_interval=refresh_interval,
        probe_factory=probe_factory,
        limit=limit,
        initial_repository_filter=initial_repository_filter,
        initial_agent_filter=initial_agent_filter,
    ).run()
    return 0

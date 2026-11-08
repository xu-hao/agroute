"""A credential-free realtime terminal view of all AGRoute model routes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import shlex
import threading
from typing import Callable

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
    "(the scriptable view is: agroute list)"
)

_COMPACT_TABLE_WIDTH = 80
_COMPACT_MODEL_WIDTH = 13
_COMPACT_LABEL_WIDTH = 8
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
    label_index = 6
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


_STATUS_STYLES = {
    "routed": "bold cyan",
    "review": "bold yellow",
    "clean": "bold green",
    "loud": "bold red",
    "silent": "bold magenta",
}


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
    outcome = row.get("outcome") or "pending"
    exec_id = row.get("execution_id") or "—"
    tokens = f"{row['tokens']:,}" if isinstance(row.get("tokens"), int) else "—"
    cost = f"${row['cost_usd']:.4f}" if type(row.get("cost_usd")) in (int, float) else "—"
    dur = f"{row['seconds']:.1f}s" if type(row.get("seconds")) in (int, float) else "—"
    return (
        f"{row['route_id']} · {row['status']} · {row['execution_mode']} "
        f"({row['host_agent']} → {row['target_agent']}) · {_repository_label(row['repository'])}\n"
        f"target: {row['qualified_model']} ({row['effort']}) · type: {row['task_type']} · "
        f"risk: {risk} · cap_met: {cap}{review}{affinity}\n"
        f"invocation: {_invocation_summary(row.get('invocation'))}\n"
        f"outcome: {outcome} · exec_id: {exec_id} · tokens: {tokens} · duration: {dur} · cost: {cost}\n"
        f"label: {row['label']}\n"
        f"routed: {row['created_at']} · observed: {row.get('observed_at') or '—'}"
    )


def _modal_detail(row: dict) -> str:
    lines = [
        f"Route ID: {row['route_id']}",
        f"Decision ID: {row.get('decision_id') or '—'}",
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
        f"Outcome: {row.get('outcome') or 'pending'}",
        f"Execution ID: {row.get('execution_id') or '—'}",
        f"Tokens: {row['tokens'] if row.get('tokens') is not None else '—'}",
        f"Seconds: {row['seconds'] if row.get('seconds') is not None else '—'}",
        f"Cost USD: {row['cost_usd'] if row.get('cost_usd') is not None else '—'}",
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
            if type(clean) in (int, float):
                lines.append(
                    f"  {model} ({eff}): clean={clean:.3f} loud={float(loud or 0):.3f} silent={float(silent or 0):.3f}"
                )
            else:
                lines.append(f"  {json.dumps(item, sort_keys=True)}")
    if row.get("note"):
        lines.append(f"\nObservation note:\n{row['note']}")
    if row.get("task_text"):
        lines.append(f"\nTask prompt excerpt:\n{row['task_text']}")
    return "\n".join(lines)


def _agent_quota_badge(name: str, info: dict) -> str:
    if not info.get("enabled", True):
        return f"{name} off"
    if not info.get("eligible_as_target", True):
        return f"{name} lim"
    s5 = (info.get("session_5h") or {}).get("used_pct")
    w7 = (info.get("weekly_7d") or {}).get("used_pct")
    if type(s5) in (int, float) or type(w7) in (int, float):
        s_txt = f"{s5:.0f}%" if type(s5) in (int, float) else "—"
        w_txt = f"{w7:.0f}%" if type(w7) in (int, float) else "—"
        return f"{name} {s_txt}/{w_txt}"
    return f"{name} ok"


def _usage_modal_body(snapshot: dict) -> str:
    lines = [f"Evaluated at: {snapshot.get('captured_at', '—')}", ""]
    agents = snapshot.get("agents") or {}
    for name in AGENTS:
        info = agents.get(name) or {}
        s5 = info.get("session_5h") or {}
        w7 = info.get("weekly_7d") or {}
        lines.append(f"[{name}]")
        lines.append(f"  Enabled: {info.get('enabled', True)}")
        lines.append(
            f"  Eligible as target: {info.get('eligible_as_target', True)}"
            + (f" ({info['target_exclusion_reason']})" if info.get("target_exclusion_reason") else "")
        )
        lines.append(
            f"  Eligible as host native: {info.get('eligible_as_host_native', True)}"
            + (f" ({info['host_native_exclusion_reason']})" if info.get("host_native_exclusion_reason") else "")
        )
        lines.append(
            f"  5h session used: {s5.get('used_pct') if s5.get('used_pct') is not None else '—'}% "
            f"(reset: {s5.get('reset_at') or '—'})"
        )
        lines.append(
            f"  7d weekly used:  {w7.get('used_pct') if w7.get('used_pct') is not None else '—'}% "
            f"(reset: {w7.get('reset_at') or '—'})"
        )
        lines.append(
            f"  Rate-limited: {info.get('rate_limited', False)} "
            f"(cooldown: {info.get('cooldown_until') or '—'})"
        )
        tiers = info.get("tiers") or {}
        if tiers:
            lines.append(f"  Tiers: {json.dumps(tiers, sort_keys=True)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def build_app(
    snapshot_factory: Callable[[], dict],
    *,
    refresh_interval: float = 1.0,
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
        #routes { height: 1fr; }
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
            Binding("h", "history", "hide/show observed"),
            Binding("p", "repository", "repository filter"),
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
            self._message = "loading …"
            self._snapshot: dict | None = None
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
                ("STATE", 7),
                ("MODE", 6),
                ("REPO", 9),
                ("AGENT", 9),
                ("MODEL", model_width),
                ("EFFORT", 6),
                ("LABEL", label_width),
                ("AGE", 5),
            ):
                table.add_column(label, width=width)
            self.action_refresh()
            self.set_interval(refresh_interval, self.action_refresh)

        def _render_resized_table(self) -> None:
            if self._snapshot is not None:
                self._render_snapshot(self._snapshot)

        def action_refresh(self) -> None:
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

        def _read_failed(self, exc: AgRouteError) -> None:
            if not self.is_running:
                return
            self._reading = False
            self._message = f"unavailable · {exc}"
            self.query_one("#route-subject", Static).update("MACHINE │ AGROUTE │ unavailable")
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

        def _render_snapshot(self, snapshot: dict) -> None:
            selected = self._selected_id()
            table = self.query_one("#routes", DataTable)
            viewport = (table.scroll_x, table.scroll_y)
            all_rows = list(snapshot.get("routes") or [])
            ordered = list(all_rows)
            if not self._show_history:
                ordered = [row for row in ordered if row["status"] not in OBSERVED_STATUSES]
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
                ordered = [row for row in ordered if row["status"] == self._state_filter]

            self._rows = {row["route_id"]: row for row in ordered}
            model_width, _ = _flexible_widths(table.size.width)
            table.ordered_columns[4].width = model_width
            table.ordered_columns[6].width = _fitted_label_width(table, len(ordered))
            table.virtual_size = table.virtual_size.with_width(
                sum(column.get_render_width(table) for column in table.ordered_columns)
            )
            now = _moment(snapshot.get("captured_at")) or datetime.now(timezone.utc)
            rendered = {
                row["route_id"]: (
                    _compact_cell(
                        row["status"],
                        style=_STATUS_STYLES.get(row["status"]),
                        route_id=row["route_id"],
                    ),
                    _compact_cell(row["execution_mode"]),
                    _compact_cell(_repository_label(row["repository"])),
                    _compact_cell(_agent_label(row)),
                    _compact_cell(row["model"]),
                    _compact_cell(_effort_label(row["effort"])),
                    _compact_cell(row["label"]),
                    _compact_cell(_age(row, now)),
                )
                for row in ordered
            }

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
            self.query_one("#route-subject", Static).update(
                f"MACHINE │ AGROUTE │ {routed_count} routed · {observed_count} observed · "
                f"{repository} · {agent} · {model} · {state} · observed {history}"
            )
            agents_info = snapshot.get("agents") or {}
            badges = " · ".join(
                _agent_quota_badge(name, agents_info.get(name) or {})
                for name in AGENTS
            )
            self._message = (
                f"{badges} · showing {len(ordered)}/{len(all_rows)} routes · "
                f"read {snapshot.get('captured_at', '—')}"
            )
            self.query_one("#route-status", Static).update(self._message)
            self._update_detail()

        def _update_detail(self) -> None:
            width = max(1, self.size.width - 2)
            self.query_one("#route-rule", Static).update("─" * width)
            row = self._selected()
            if row is None:
                self.query_one("#route-detail", Static).update(
                    "no matching routes"
                    if self._snapshot is not None
                    else "loading AGRoute …"
                )
                return
            self.query_one("#route-detail", Static).update(_detail_text(row))

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
                )
            )

        def _filter_rows(self) -> list[dict]:
            if self._snapshot is None:
                return []
            rows = list(self._snapshot.get("routes") or [])
            if not self._show_history:
                rows = [r for r in rows if r["status"] not in OBSERVED_STATUSES]
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
            states = {str(row["status"]) for row in self._filter_rows()}
            states.update(("routed", "review", "clean", "loud", "silent"))
            if self._state_filter is not None:
                states.add(self._state_filter)
            choices = [(st, st) for st in sorted(states)]
            self.push_screen(
                FilterPicker(
                    "State filter",
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
                    + "\n\nrouted   decision/plan recorded, awaiting execution feedback"
                    + "\nreview   silent-risk cap unmet or review required"
                    + "\nclean    observed execution completed without rework or escaped defect"
                    + "\nloud     observed execution required caught rework/retry"
                    + "\nsilent   observed execution had an escaped defect",
                )
            )

    return RouterApp()


def run(
    snapshot_factory: Callable[[], dict],
    *,
    refresh_interval: float = 1.0,
    initial_repository_filter: str | None = None,
    initial_agent_filter: str | None = None,
) -> int:
    if App is None:  # pragma: no cover
        print(_MISSING)
        return 2
    build_app(
        snapshot_factory,
        refresh_interval=refresh_interval,
        initial_repository_filter=initial_repository_filter,
        initial_agent_filter=initial_agent_filter,
    ).run()
    return 0

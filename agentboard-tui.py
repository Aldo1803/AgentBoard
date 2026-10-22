# /// script
# requires-python = ">=3.12"
# dependencies = ["textual>=8.2,<9"]
# ///
"""AgentBoard console - the operator's view of a running AgentBoard daemon.

The daemon stays the source of truth; this console is a thin HTTP client of it.
Run `uv run agentboard.py serve` first, then `uv run agentboard-tui.py`.
"""

import argparse
import asyncio
import json
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from rich.markup import escape
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    OptionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

STATE_ORDER = {"waiting": 0, "ready": 1, "claimed": 2, "in_review": 3, "blocked": 4, "done": 5}
STATE_STYLE = {
    "waiting": "yellow",
    "ready": "green",
    "claimed": "cyan",
    "in_review": "magenta",
    "blocked": "red",
    "done": "dim",
}
STATE_FILTERS = ["all", "ready", "waiting", "claimed", "in_review", "blocked", "done"]


def ago(value):
    if not value:
        return "-"
    text = str(value).strip()
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        then = datetime.fromisoformat(text)
    except ValueError:
        return "-"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    seconds = int((datetime.now(timezone.utc) - then).total_seconds())
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d ago"


def detail_facts_text(detail):
    state = detail["state"]
    lines = [f"[b]{escape(detail['id'])}[/b]  {escape(detail['title'])}"]
    state_line = f"state: [b]{escape(state)}[/b]"
    if state == "claimed":
        state_line += f" by {escape(detail.get('agent') or '?')} ({ago(detail.get('last_event_at'))})"
    lines.append(state_line)
    if detail.get("group"):
        lines.append(f"group: {escape(detail['group'])}")
    if detail.get("source"):
        lines.append(f"source: {escape(detail['source'])}")
    if detail.get("scope"):
        lines.append("scope: " + escape(", ".join(detail["scope"])))
    lines.append(f"branch: {escape(detail.get('branch') or '-')}")
    if detail.get("pr"):
        lines.append(f"PR: {escape(detail['pr'])}")
    if detail.get("blocked_reason"):
        lines.append(f"blocked: {escape(detail['blocked_reason'])}")
    lines.append(f"created {detail.get('created_at', '')}  updated {detail.get('updated_at', '')}")
    if detail.get("done_at"):
        lines.append(f"done {detail['done_at']}")
    return "\n".join(lines)


def detail_deps_text(detail):
    lines = ["[b]Depends on[/b]"]
    deps = detail.get("depends_on") or []
    if not deps:
        lines.append("  (nothing)")
    for dep in deps:
        mark = "done" if dep["status"] == "done" else "open"
        title = escape(dep.get("title") or "")
        lines.append(f"  {escape(dep['id']):<10} {mark:<6} {title}")
    return "\n".join(lines)


def detail_events_text(detail):
    lines = ["[b]Events[/b] (newest first)"]
    events = detail.get("events") or []
    if not events:
        lines.append("  (none)")
    for event in events[:20]:
        bits = " ".join(f"{key}={value}" for key, value in (event.get("detail") or {}).items())
        lines.append(
            f"  #{event['id']}  {ago(event['at']):>9}  "
            f"{escape(event['actor'])}  {escape(event['kind'])}  {escape(bits)}"
        )
    return "\n".join(lines)


class Api:
    def __init__(self, base_url):
        self.base_url = base_url.rstrip("/")

    def get(self, path):
        request = urllib.request.Request(
            self.base_url + path, headers={"Accept": "application/json"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def post(self, path, data=None):
        body = urllib.parse.urlencode(data or {}).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception as exc:
            return 0, f"Could not reach the board: {exc}"


class PromptScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, title, prompt, placeholder=""):
        super().__init__()
        self.prompt_title = title
        self.prompt_text = prompt
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.prompt_title, classes="dialog-title")
            yield Label(self.prompt_text, classes="dialog-hint")
            yield Input(placeholder=self.placeholder, id="dialog-input")
            yield Label("enter to confirm · esc to cancel", classes="dialog-hint")

    def on_mount(self):
        self.query_one("#dialog-input", Input).focus()

    @on(Input.Submitted)
    def submitted(self, event):
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self):
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "yes", "yes"),
        Binding("n", "no", "no"),
        Binding("enter", "yes", "yes", show=False),
        Binding("escape", "no", "cancel"),
    ]

    def __init__(self, title, body):
        super().__init__()
        self.confirm_title = title
        self.confirm_body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.confirm_title, classes="dialog-title")
            yield Label(self.confirm_body, classes="dialog-hint")
            yield Label("y to confirm · n to cancel", classes="dialog-hint")

    def action_yes(self):
        self.dismiss(True)

    def action_no(self):
        self.dismiss(False)


class ResultScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("enter", "close", "close"),
    ]

    def __init__(self, title, body):
        super().__init__()
        self.result_title = title
        self.result_body = body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(self.result_title, classes="dialog-title")
            with VerticalScroll(classes="result"):
                yield Static(self.result_body, markup=False)
            yield Label("enter / esc to close", classes="dialog-hint")

    def action_close(self):
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "close"),
        Binding("enter", "close", "close"),
        Binding("question_mark", "close", "close", show=False),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label("AgentBoard console - keys", classes="dialog-title")
            with VerticalScroll(classes="result"):
                yield Static(
                    "[b]j / k, arrows[/b]   move through cards\n"
                    "[b]a / d[/b]           previous / next tab (Board, Groups, Activity)\n"
                    "[b]1 / 2 / 3[/b]       Board / Groups / Activity\n"
                    "[b]enter[/b]           actions for the card, or open the group in Board\n"
                    "[b]f or /[/b]          filter cards (text)\n"
                    "[b]g[/b]               cycle state filter (all, ready, ...)\n"
                    "[b]H[/b]               show / hide done cards\n"
                    "[b]s[/b]               sync merges now\n"
                    "[b]r[/b]               refresh from the board\n"
                    "[b]h[/b]               this help\n"
                    "[b]q[/b]               quit\n\n"
                    "Groups clusters cards by their group; Enter opens the selected\n"
                    "group in Board. Actions are recorded by the board with actor\n"
                    "'person': claim for an agent, release, unblock, mark merged.\n"
                    "Agents keep using the HTTP API; this console never runs an\n"
                    "agent and never changes git.",
                    markup=True,
                )
            yield Label("enter / esc to close", classes="dialog-hint")

    def action_close(self):
        self.dismiss(None)


class ActionScreen(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, card, options):
        super().__init__()
        self.card = card
        self.options_list = options

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"{self.card['id']} - actions", classes="dialog-title")
            yield OptionList(
                *[Option(label, id=key) for label, key in self.options_list],
                id="action-list",
            )
            yield Label("enter to choose · esc to cancel", classes="dialog-hint")

    def on_mount(self):
        self.query_one("#action-list", OptionList).focus()

    @on(OptionList.OptionSelected)
    def chosen(self, event):
        self.dismiss(event.option.id)

    def action_cancel(self):
        self.dismiss(None)


class BoardApp(App):
    TITLE = "AgentBoard"
    SUB_TITLE = "console"
    CSS = """
    Screen { layout: vertical; }
    Header { height: 1; }
    #statusbar { dock: top; height: 1; background: $boost; color: $text-muted; padding: 0 1; }
    TabbedContent { height: 1fr; }
    TabPane { height: 1fr; padding: 0; }
    #filter { display: none; height: 3; margin: 0 1; }
    #board-body { height: 1fr; }
    #cards { width: 58%; height: 1fr; }
    #detail { width: 42%; height: 1fr; padding: 0 1; border-left: solid $panel; }
    #detail-facts { margin-bottom: 1; }
    #detail-body { margin-bottom: 1; }
    #activity-body { height: 1fr; padding: 0 1; }
    #agents { width: 34%; height: 1fr; }
    #activity-scroll { width: 66%; height: 1fr; padding: 0 1; border-left: solid $panel; }
    #groups-body { height: 1fr; padding: 0 1; }
    #groups { width: 56%; height: 1fr; }
    #group-detail { width: 44%; height: 1fr; padding: 0 1; border-left: solid $panel; }
    #group-summary { margin-bottom: 1; }
    .dialog { width: 72; height: auto; max-height: 20; padding: 1 2; background: $panel;
              border: thick $primary; }
    .dialog.wide { width: 96; max-height: 28; }
    .dialog-title { text-style: bold; margin-bottom: 1; }
    .dialog-hint { color: $text-muted; }
    .result { height: auto; max-height: 18; padding: 1 0; }
    PromptScreen, ConfirmScreen, ResultScreen, HelpScreen, ActionScreen {
      align: center middle;
    }
    """
    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "refresh", "refresh"),
        Binding("s", "sync", "sync"),
        Binding("f", "focus_filter", "filter"),
        Binding("/", "focus_filter", "filter", show=False),
        Binding("g", "cycle_state", "state"),
        Binding("a", "tab_left", "a/d tabs"),
        Binding("d", "tab_right", "", show=False),
        Binding("enter", "open", "open"),
        Binding("H", "toggle_done", "done"),
        Binding("h", "help", "help"),
        Binding("1", "show_board", "board", show=False),
        Binding("2", "show_groups", "groups", show=False),
        Binding("3", "show_activity", "activity", show=False),
        Binding("escape", "clear_ui", "", show=False),
    ]

    TAB_IDS = ("tab-board", "tab-groups", "tab-activity")
    TAB_TABLES = {"tab-board": "#cards", "tab-groups": "#groups", "tab-activity": "#agents"}

    def __init__(self, base_url):
        super().__init__()
        self.api = Api(base_url)
        self.snapshot = None
        self.detail = None
        self.events = []
        self.selected_id = None
        self.selected_group = None
        self.group_names = []
        self.groups = {}
        self.state_filter = "all"
        self.text_filter = ""
        self.group_filter = None
        self.hide_done = True
        self.activity_agent = None
        self.online = False
        self._last_refresh = 0.0
        self._visible = []
        self._stop_stream = threading.Event()
        self._stream_thread = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static("connecting…", id="statusbar")
        with TabbedContent():
            with TabPane("Board", id="tab-board"):
                yield Input(placeholder="filter by id, title, agent, group  (esc clears)", id="filter")
                with Horizontal(id="board-body"):
                    yield DataTable(id="cards")
                    with VerticalScroll(id="detail"):
                        yield Static(id="detail-facts")
                        yield Markdown(id="detail-body")
                        yield Static(id="detail-deps")
                        yield Static(id="detail-events")
            with TabPane("Groups", id="tab-groups"):
                with Horizontal(id="groups-body"):
                    yield DataTable(id="groups")
                    with VerticalScroll(id="group-detail"):
                        yield Static(id="group-summary")
                        yield Static(id="group-cards")
            with TabPane("Activity", id="tab-activity"):
                with Horizontal(id="activity-body"):
                    yield DataTable(id="agents")
                    with VerticalScroll(id="activity-scroll"):
                        yield Static(id="activity-summary")
                        yield Static(id="activity-events")
        yield Footer()

    def on_mount(self):
        cards = self.query_one("#cards", DataTable)
        cards.add_columns("State", "ID", "Title", "Who", "Age")
        cards.cursor_type = "row"
        cards.zebra_stripes = True
        groups = self.query_one("#groups", DataTable)
        groups.add_columns("Group", "Cards", "Wait", "Ready", "Claim", "Review", "Block", "Done")
        groups.cursor_type = "row"
        groups.zebra_stripes = True
        agents = self.query_one("#agents", DataTable)
        agents.add_columns("Agent", "Holds", "Last event", "State")
        agents.cursor_type = "row"
        cards.focus()
        self.refresh_data(force=True)
        self._stream_thread = threading.Thread(target=self._stream_loop, daemon=True)
        self._stream_thread.start()
        self.set_interval(5, self.refresh_if_due)

    def on_unmount(self):
        self._stop_stream.set()

    def refresh_if_due(self):
        if time.monotonic() - self._last_refresh >= 4:
            self.refresh_data()

    def refresh_data(self, force=False):
        if not force and time.monotonic() - self._last_refresh < 0.6:
            return
        try:
            snapshot = self.api.get("/board")
            events_payload = self.api.get("/events?format=json")
        except Exception:
            self.online = False
            self.update_statusbar()
            return
        self.snapshot = snapshot
        self.events = (events_payload.get("events") or [])[-120:]
        self.online = True
        self._last_refresh = time.monotonic()
        self.rebuild_cards()
        self.rebuild_groups()
        self.rebuild_agents()
        self.update_statusbar()
        self.update_detail()

    def _stream_loop(self):
        while not self._stop_stream.is_set():
            try:
                request = urllib.request.Request(self.api.base_url + "/events/stream")
                with urllib.request.urlopen(request, timeout=20) as response:
                    event_name = None
                    for raw in response:
                        if self._stop_stream.is_set():
                            return
                        line = raw.decode("utf-8", "replace").strip()
                        if line.startswith("event:"):
                            event_name = line.split(":", 1)[1].strip()
                        elif line.startswith("data:") and event_name:
                            try:
                                self.call_from_thread(self.on_stream_event, event_name)
                            except Exception:
                                return
                            event_name = None
            except Exception:
                pass
            self._stop_stream.wait(3)

    def on_stream_event(self, event_name):
        if event_name in ("snapshot", "event"):
            self.refresh_data()

    def filtered_cards(self):
        cards = (self.snapshot or {}).get("cards") or []
        if self.hide_done:
            cards = [card for card in cards if card["state"] != "done"]
        if self.group_filter:
            if self.group_filter == "(ungrouped)":
                cards = [card for card in cards if not card.get("group")]
            else:
                cards = [card for card in cards if card.get("group") == self.group_filter]
        if self.state_filter != "all":
            cards = [card for card in cards if card["state"] == self.state_filter]
        needle = self.text_filter.strip().lower()
        if needle:
            def haystack(card):
                return " ".join(
                    str(card.get(key) or "")
                    for key in ("id", "title", "agent", "group", "source")
                ).lower()
            cards = [card for card in cards if needle in haystack(card)]
        return sorted(
            cards, key=lambda card: (STATE_ORDER.get(card["state"], 9), card.get("seq") or 0)
        )

    def who(self, card):
        agent = card.get("agent") or ""
        if card["state"] == "claimed" and card.get("stale"):
            return f"{agent} · stale"
        return agent

    def age_label(self, card):
        if card["state"] in ("claimed", "in_review"):
            return ago(card.get("last_event_at"))
        if card["state"] == "done":
            return ago(card.get("done_at"))
        return ""

    def rebuild_cards(self):
        table = self.query_one("#cards", DataTable)
        selected = self.selected_id
        table.clear()
        cards = self.filtered_cards()
        self._visible = [card["id"] for card in cards]
        for card in cards:
            table.add_row(
                Text(card["state"], style=STATE_STYLE.get(card["state"], "")),
                card["id"],
                card["title"],
                self.who(card),
                self.age_label(card),
                key=card["id"],
            )
        if selected and selected in self._visible:
            index = self._visible.index(selected)
            table.move_cursor(row=index)
            self.selected_id = selected
        elif self._visible:
            table.move_cursor(row=0)
            self.selected_id = self._visible[0]
        else:
            self.selected_id = None
            self.detail = None
            self.clear_detail()

    def rebuild_groups(self):
        table = self.query_one("#groups", DataTable)
        previous = self.selected_group
        table.clear()
        groups = {}
        for card in (self.snapshot or {}).get("cards") or []:
            name = card.get("group") or "(ungrouped)"
            groups.setdefault(name, []).append(card)
        self.groups = groups
        self.group_names = sorted(groups, key=lambda name: str(name).lower())
        for name in self.group_names:
            cards = groups[name]
            counts = {key: 0 for key in ("waiting", "ready", "claimed", "in_review", "blocked", "done")}
            for card in cards:
                state = card["state"]
                if state in counts:
                    counts[state] += 1
            table.add_row(
                name,
                str(len(cards)),
                str(counts["waiting"]),
                str(counts["ready"]),
                str(counts["claimed"]),
                str(counts["in_review"]),
                str(counts["blocked"]),
                str(counts["done"]),
                key=name,
            )
        if previous and previous in self.group_names:
            table.move_cursor(row=self.group_names.index(previous))
            self.selected_group = previous
        elif self.group_names:
            table.move_cursor(row=0)
            self.selected_group = self.group_names[0]
        else:
            self.selected_group = None
        self.render_group_detail()

    def render_group_detail(self):
        name = self.selected_group
        summary = self.query_one("#group-summary", Static)
        cards_view = self.query_one("#group-cards", Static)
        if not name:
            summary.update("")
            cards_view.update("")
            return
        cards = list(self.groups.get(name, []))
        visible = [card for card in cards if not self.hide_done or card["state"] != "done"]
        plural = "card" if len(cards) == 1 else "cards"
        text = f"[b]{escape(name)}[/b] · {len(cards)} {plural}"
        if len(visible) != len(cards):
            text += f" · {len(cards) - len(visible)} done hidden"
        summary.update(text)
        lines = []
        for card in sorted(
            visible, key=lambda card: (STATE_ORDER.get(card["state"], 9), card.get("seq") or 0)
        ):
            style = STATE_STYLE.get(card["state"]) or "white"
            line = (
                f"[{style}]{card['state']:<9}[/] "
                f"[b]{escape(card['id'])}[/b]  {escape(card['title'])}"
            )
            if card.get("agent") and card["state"] in ("claimed", "in_review"):
                line += f"  [dim]{escape(card['agent'])}[/dim]"
            if card["state"] == "blocked" and card.get("blocked_reason"):
                line += f"  [dim]{escape(card['blocked_reason'])}[/dim]"
            lines.append(line)
        if not lines:
            lines.append("[dim](nothing to show)[/dim]")
        cards_view.update("\n".join(lines))

    def rebuild_agents(self):
        table = self.query_one("#agents", DataTable)
        previous = self.activity_agent
        table.clear()
        holds = {}
        for card in (self.snapshot or {}).get("cards") or []:
            if card["state"] in ("claimed", "in_review") and card.get("agent"):
                holds.setdefault(card["agent"], []).append(card)
        actors = {}
        for event in self.events:
            info = actors.setdefault(event["actor"], {"last": None})
            if info["last"] is None or event["at"] > info["last"]:
                info["last"] = event["at"]
        names = sorted(
            (set(holds) | {name for name in actors if name != "board"}),
            key=lambda name: str(name).lower(),
        )
        for name in names:
            held = holds.get(name, [])
            stale = any(card.get("stale") for card in held)
            state = "stale" if stale else ("active" if held else "idle")
            table.add_row(
                name,
                str(len(held)),
                ago(actors.get(name, {}).get("last")),
                state,
                key=name,
            )
        self.activity_agent = previous
        self.render_activity()

    @on(DataTable.RowHighlighted, "#cards")
    def card_highlighted(self, event):
        if event.row_key is None:
            return
        key = event.row_key.value
        if key:
            self.selected_id = key
            self.update_detail()

    @on(DataTable.RowHighlighted, "#agents")
    def agent_highlighted(self, event):
        if event.row_key is None:
            return
        key = event.row_key.value
        self.activity_agent = key
        self.render_activity()

    @on(DataTable.RowHighlighted, "#groups")
    def group_highlighted(self, event):
        if event.row_key is None:
            return
        key = event.row_key.value
        if key:
            self.selected_group = key
            self.render_group_detail()

    @on(DataTable.RowSelected, "#cards")
    def card_selected(self, event):
        if event.row_key is not None and event.row_key.value:
            self.selected_id = event.row_key.value
        self.action_actions()

    @on(DataTable.RowSelected, "#groups")
    def group_selected(self, event):
        if event.row_key is not None and event.row_key.value:
            self.selected_group = event.row_key.value
        self.open_selected_group()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event):
        self.text_filter = event.value
        self.rebuild_cards()
        self.update_statusbar()

    def update_detail(self):
        if not self.selected_id:
            self.clear_detail()
            return
        try:
            detail = self.api.get(f"/cards/{urllib.parse.quote(self.selected_id)}")
        except Exception:
            self.clear_detail()
            return
        self.detail = detail
        self.query_one("#detail-facts", Static).update(detail_facts_text(detail))
        self.query_one("#detail-body", Markdown).update(detail.get("body") or "(no body)")
        self.query_one("#detail-deps", Static).update(detail_deps_text(detail))
        self.query_one("#detail-events", Static).update(detail_events_text(detail))

    def clear_detail(self):
        self.query_one("#detail-facts", Static).update("")
        self.query_one("#detail-body", Markdown).update("")
        self.query_one("#detail-deps", Static).update("")
        self.query_one("#detail-events", Static).update("")

    def render_activity(self):
        agent = self.activity_agent
        events = list(reversed(self.events))
        if agent:
            events = [event for event in events if event["actor"] == agent]
        summary = "All agents" if not agent else agent
        summary += f" · {len(events)} of the last {len(self.events)} events"
        self.query_one("#activity-summary", Static).update(summary)
        lines = []
        for event in events[:40]:
            bits = " ".join(f"{key}={value}" for key, value in (event.get("detail") or {}).items())
            lines.append(
                f"{ago(event['at']):>9}  {event['actor']:<16} {event['kind']:<10} "
                f"{event['card_id'] or '-':<10} {bits}"
            )
        if not lines:
            lines.append("(no events)")
        self.query_one("#activity-events", Static).update(Text("\n".join(lines)))

    def update_statusbar(self):
        parts = []
        if self.online:
            parts.append(Text("● online", style="green"))
        else:
            parts.append(Text("● offline", style="red"))
        if self.snapshot:
            counts = self.snapshot.get("counts") or {}
            bits = [self.snapshot.get("repo_name") or self.snapshot.get("repo") or "?"]
            bits.append(str(self.snapshot.get("base") or "?"))
            last = self.snapshot.get("last_sync_at")
            bits.append("sync " + (ago(last) if last else "never"))
            bits.append(
                "ready {ready} · claimed {claimed} · review {in_review} · blocked {blocked} · done {done}".format(
                    ready=counts.get("ready", 0),
                    claimed=counts.get("claimed", 0),
                    in_review=counts.get("in_review", 0),
                    blocked=counts.get("blocked", 0),
                    done=counts.get("done", 0),
                )
            )
            if self.hide_done:
                bits.append("done hidden")
            if self.state_filter != "all":
                bits.append(f"state:{self.state_filter}")
            if self.group_filter:
                bits.append(f"group:{self.group_filter}")
            if self.text_filter:
                bits.append(f"filter:{self.text_filter}")
            parts.append(Text("  |  ".join(bits), style="dim"))
        else:
            parts.append(Text(f"  cannot reach {self.api.base_url}", style="dim"))
        bar = Text()
        for index, part in enumerate(parts):
            if index:
                bar.append("  ")
            bar.append_text(part)
        self.query_one("#statusbar", Static).update(bar)

    def action_refresh(self):
        self.refresh_data(force=True)

    def _show_tab(self, tab_id):
        self.query_one(TabbedContent).active = tab_id
        table_id = self.TAB_TABLES.get(tab_id)
        if table_id:
            self.call_after_refresh(lambda: self.query_one(table_id, DataTable).focus())

    def action_show_board(self):
        self._show_tab("tab-board")

    def action_show_groups(self):
        self._show_tab("tab-groups")

    def action_show_activity(self):
        self._show_tab("tab-activity")

    def action_tab_left(self):
        tabs = self.query_one(TabbedContent)
        try:
            index = self.TAB_IDS.index(tabs.active)
        except ValueError:
            index = 0
        self._show_tab(self.TAB_IDS[(index - 1) % len(self.TAB_IDS)])

    def action_tab_right(self):
        tabs = self.query_one(TabbedContent)
        try:
            index = self.TAB_IDS.index(tabs.active)
        except ValueError:
            index = 0
        self._show_tab(self.TAB_IDS[(index + 1) % len(self.TAB_IDS)])

    def action_open(self):
        tabs = self.query_one(TabbedContent)
        if tabs.active == "tab-groups":
            self.open_selected_group()
        elif tabs.active == "tab-board":
            self.action_actions()
        else:
            self.notify("Switch to Board or Groups to open something.")

    def open_selected_group(self):
        if not self.selected_group:
            return
        cards = self.groups.get(self.selected_group, [])
        if self.hide_done and cards and all(card["state"] == "done" for card in cards):
            self.notify("every card in this group is done - H shows them", timeout=6)
        self.group_filter = self.selected_group
        self.state_filter = "all"
        self.action_show_board()
        self.rebuild_cards()
        self.update_statusbar()
        self.notify(f"group: {self.selected_group}")

    def action_toggle_done(self):
        self.hide_done = not self.hide_done
        self.rebuild_cards()
        self.render_group_detail()
        self.update_statusbar()
        self.notify("done cards hidden" if self.hide_done else "done cards shown")

    def action_focus_filter(self):
        box = self.query_one("#filter", Input)
        box.display = True
        box.focus()

    def action_clear_ui(self):
        box = self.query_one("#filter", Input)
        changed = False
        if box.display:
            box.value = ""
            box.display = False
            self.text_filter = ""
            changed = True
        if self.group_filter:
            self.group_filter = None
            changed = True
        if changed:
            self.action_show_board()
            self.rebuild_cards()
            self.update_statusbar()
            return
        self.query_one("#cards", DataTable).focus()

    def action_cycle_state(self):
        index = STATE_FILTERS.index(self.state_filter)
        self.state_filter = STATE_FILTERS[(index + 1) % len(STATE_FILTERS)]
        self.rebuild_cards()
        self.update_statusbar()
        self.notify(f"state filter: {self.state_filter}")

    def action_help(self):
        self.push_screen(HelpScreen())

    def action_sync(self):
        self.notify("Syncing with the remote…")
        self.run_worker(self._sync_worker, thread=True, exclusive=True)

    def _sync_worker(self):
        code, text = self.api.post("/sync")
        try:
            self.call_from_thread(self._after_sync, code, text)
        except Exception:
            pass

    def _after_sync(self, code, text):
        if code == 200:
            self.notify(text.strip() or "sync done")
            self.refresh_data(force=True)
        else:
            self.notify(text.strip() or f"sync failed ({code})", severity="error", timeout=8)

    def action_actions(self):
        if not self.detail:
            self.notify("Select a card first.")
            return
        card = self.detail
        options = []
        if card["state"] == "ready":
            options.append(("Claim for an agent…", "claim"))
        if card["state"] == "claimed":
            options.append(("Release to ready", "release"))
        if card["state"] == "blocked":
            options.append(("Unblock with a note…", "unblock"))
        if card["state"] == "in_review":
            options.append(("Mark merged (squash)…", "merged"))
        if not options:
            self.notify(f"No console action for a {card['state']} card.")
            return
        self.push_screen(ActionScreen(card, options), self._after_action_choice)

    def _after_action_choice(self, action):
        if not action or not self.detail:
            return
        card_id = self.detail["id"]
        if action == "claim":
            self.push_screen(
                PromptScreen("Claim for an agent", "Agent name to record for the claim:", "claude-code"),
                lambda value: self._submit("claim", card_id, {"agent": value}) if value else None,
            )
        elif action == "release":
            self.push_screen(
                ConfirmScreen(
                    f"Release {card_id}?",
                    "The card returns to ready. The branch and worktree stay.",
                ),
                lambda ok: self._submit("release", card_id, {}) if ok else None,
            )
        elif action == "unblock":
            self.push_screen(
                PromptScreen("Unblock", "The person's note (recorded in the event log):"),
                lambda value: self._submit("unblock", card_id, {"note": value}) if value else None,
            )
        elif action == "merged":
            self.push_screen(
                PromptScreen("Mark merged", "Note (who confirmed the merge):", "squashed, confirmed in GitHub"),
                lambda value: self._submit("merged", card_id, {"note": value}) if value else None,
            )

    def _submit(self, action, card_id, data):
        self.run_worker(
            lambda: self._post_action(action, card_id, data), thread=True, exclusive=True
        )

    def _post_action(self, action, card_id, data):
        code, text = self.api.post(f"/cards/{urllib.parse.quote(card_id)}/{action}", data)
        try:
            self.call_from_thread(self._after_action, action, card_id, code, text)
        except Exception:
            pass

    def _after_action(self, action, card_id, code, text):
        if code == 200:
            if action == "claim":
                self.push_screen(ResultScreen(f"{card_id} claimed", text.strip()))
            else:
                self.notify(text.strip().splitlines()[0] if text.strip() else "done")
            self.refresh_data(force=True)
        else:
            self.notify(text.strip() or f"refused ({code})", severity="error", timeout=8)


def _build_stub_handler():
    from http.server import BaseHTTPRequestHandler

    cards = [
        {
            "id": "IDN-01",
            "seq": 1,
            "title": "Bootstrapping",
            "group": "identity",
            "state": "ready",
            "status": "todo",
            "agent": None,
            "branch": None,
            "pr": None,
            "blocked_reason": None,
            "stale": False,
            "last_event_at": "2026-10-15T03:00:00Z",
            "done_at": None,
            "updated_at": "2026-10-15T03:00:00Z",
            "created_at": "2026-10-15T03:00:00Z",
            "source": "docs/spec.md",
            "depends_on": [],
            "waits_on": [],
        },
        {
            "id": "IDN-02",
            "seq": 2,
            "title": "OIDC sign-in",
            "group": "identity",
            "state": "claimed",
            "status": "claimed",
            "agent": "claude-code",
            "branch": "card/IDN-02",
            "pr": None,
            "blocked_reason": None,
            "stale": False,
            "last_event_at": "2026-10-15T03:05:00Z",
            "done_at": None,
            "updated_at": "2026-10-15T03:05:00Z",
            "created_at": "2026-10-15T03:00:00Z",
            "source": "docs/spec.md",
            "depends_on": [{"id": "IDN-01", "title": "Bootstrapping", "status": "todo"}],
            "waits_on": [{"id": "IDN-01", "title": "Bootstrapping", "status": "todo"}],
        },
        {
            "id": "IDN-03",
            "seq": 3,
            "title": "Session cookies",
            "group": "api",
            "state": "done",
            "status": "done",
            "agent": None,
            "branch": "card/IDN-03",
            "pr": "https://example.invalid/pr/3",
            "blocked_reason": None,
            "stale": False,
            "last_event_at": "2026-10-15T03:07:00Z",
            "done_at": "2026-10-15T03:07:00Z",
            "updated_at": "2026-10-15T03:07:00Z",
            "created_at": "2026-10-15T03:00:00Z",
            "source": "docs/spec.md",
            "depends_on": [],
            "waits_on": [],
        },
    ]
    snapshot = {
        "repo": "/tmp/stub-repo",
        "repo_name": "stub-repo",
        "base": "origin/main",
        "last_sync_at": "2026-10-15T03:10:00Z",
        "base_head": {"sha": "abc1234", "when": "2026-10-15T03:00:00Z", "subject": "init"},
        "counts": {"waiting": 0, "ready": 1, "claimed": 1, "in_review": 0, "blocked": 0, "done": 1},
        "max_event_id": 3,
        "cards": cards,
    }
    details = {}
    for card in cards:
        detail = dict(card)
        detail["body"] = f"Acceptance for {card['id']}."
        detail["scope"] = ["docs/**"]
        detail["events"] = [
            {
                "id": 1,
                "card_id": card["id"],
                "at": "2026-10-15T03:00:00Z",
                "actor": "tester",
                "kind": "imported",
                "detail": {"source": "docs/spec.md"},
            }
        ]
        details[card["id"]] = detail
    events = [
        {
            "id": 1,
            "card_id": "IDN-01",
            "at": "2026-10-15T03:00:00Z",
            "actor": "tester",
            "kind": "imported",
            "detail": {"source": "docs/spec.md"},
        },
        {
            "id": 2,
            "card_id": "IDN-02",
            "at": "2026-10-15T03:05:00Z",
            "actor": "claude-code",
            "kind": "claimed",
            "detail": {"branch": "card/IDN-02"},
        },
        {
            "id": 3,
            "card_id": "IDN-03",
            "at": "2026-10-15T03:07:00Z",
            "actor": "person",
            "kind": "merged",
            "detail": {"note": "squashed, confirmed in GitHub", "manual": True},
        },
    ]

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            return

        def _send_json(self, status, payload):
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/board":
                return self._send_json(200, snapshot)
            if path == "/events":
                return self._send_json(200, {"events": events})
            if path.startswith("/cards/"):
                card_id = urllib.parse.unquote(path.split("/", 2)[2])
                detail = details.get(card_id)
                if detail is None:
                    return self._send_json(404, {"error": "no card"})
                return self._send_json(200, detail)
            if path == "/events/stream":
                body = (
                    "retry: 2000\n\n"
                    "event: snapshot\ndata: " + json.dumps(snapshot) + "\n\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            return self._send_json(404, {"error": "no route"})

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self._send_json(200, {"ok": True})

    return Handler


async def _check(app, expected=None):
    async with app.run_test(size=(140, 45)) as pilot:
        await pilot.pause(1.0)
        from textual.widgets import TabbedContent

        table = app.query_one("#cards", DataTable)
        tabs = app.query_one(TabbedContent)
        if expected is not None:
            assert table.row_count == expected, f"expected {expected} cards, got {table.row_count}"
            await pilot.press("H")
            await pilot.pause(0.3)
            assert table.row_count == expected + 1, "H did not reveal done cards"
            await pilot.press("H")
            await pilot.pause(0.3)
            assert table.row_count == expected, "H did not hide done cards"
        else:
            assert app.online, "the console did not reach the board"
        if table.row_count:
            await pilot.press("down")
            await pilot.pause(0.3)
            assert app.selected_id, "no card selected after moving the cursor"
            assert app.detail is not None, "the detail pane did not load"
            assert app.detail["id"] == app.selected_id
            await pilot.press("up")
            await pilot.pause(0.2)
            await pilot.press("up")
            await pilot.pause(0.2)
            assert app.selected_id, "the selection was lost after moving above the first row"
        before = table.row_count
        app.text_filter = "no-such-card-anywhere"
        app.rebuild_cards()
        await pilot.pause(0.2)
        assert table.row_count == 0, "the text filter did not apply"
        app.text_filter = ""
        app.rebuild_cards()
        await pilot.pause(0.2)
        assert table.row_count == before, "clearing the filter did not restore the cards"

        await pilot.press("2")
        await pilot.pause(0.3)
        assert tabs.active == "tab-groups", f"pressing 2 left {tabs.active} active"
        groups = app.query_one("#groups", DataTable)
        if groups.row_count:
            await pilot.press("down")
            await pilot.pause(0.2)
            chosen = app.selected_group
            await pilot.press("enter")
            await pilot.pause(0.3)
            assert tabs.active == "tab-board", f"enter left {tabs.active} active"
            assert app.group_filter == chosen, (app.group_filter, chosen)
            assert table.row_count >= 1, "the group filter hid everything"
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert app.group_filter is None, "escape did not clear the group filter"

        for key, expected_tab in (
            ("d", "tab-groups"),
            ("d", "tab-activity"),
            ("d", "tab-board"),
            ("a", "tab-activity"),
            ("a", "tab-groups"),
            ("a", "tab-board"),
        ):
            await pilot.press(key)
            await pilot.pause(0.25)
            assert tabs.active == expected_tab, f"{key} left {tabs.active}, expected {expected_tab}"

        await pilot.press("h")
        await pilot.pause(0.3)
        assert isinstance(app.screen, HelpScreen), f"h opened {type(app.screen).__name__}"
        await pilot.press("escape")
        await pilot.pause(0.3)
        assert not isinstance(app.screen, HelpScreen), "escape did not close help"


def run_selftest(url=None):
    from http.server import ThreadingHTTPServer

    if url:
        try:
            asyncio.run(_check(BoardApp(url)))
            print("agentboard-tui selftest: ok (live board)")
            return 0
        except Exception:
            traceback.print_exc()
            print("agentboard-tui selftest: FAILED (live board)")
            return 1

    server = ThreadingHTTPServer(("127.0.0.1", 0), _build_stub_handler())
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        asyncio.run(_check(BoardApp(base), expected=2))
        print("agentboard-tui selftest: ok (stubbed board)")
        return 0
    except Exception:
        traceback.print_exc()
        print("agentboard-tui selftest: FAILED")
        return 1
    finally:
        server.shutdown()
        server.server_close()


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    parser = argparse.ArgumentParser(
        prog="agentboard-tui.py",
        description="AgentBoard console - a Textual operator view of a running AgentBoard daemon.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help="the board's address (default: http://127.0.0.1:4300)",
    )
    parser.add_argument("--selftest", action="store_true", help="run the console's built-in checks")
    args = parser.parse_args(argv)
    if args.selftest:
        return run_selftest(args.url)
    BoardApp(args.url or "http://127.0.0.1:4300").run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""hubra tui: one full-screen view of the hubra-app repos and the running Hubra.

Start it with `hubra tui`; it needs the [tui] extra (README › Install) or
`pipx inject hubra-cli textual`. Tabs:

  Repos          version on main, latest release, what's left to release, PRs, CI
  Pull requests  every open PR; the right side shows the selected one in depth
  Runs           runs waiting for your approval (signing, outside contributors),
                 then the newest run per repo; the tab and header show the count
  Security       open Dependabot, secret-scanning and code-scanning alerts
  Checks         plugin lists against the releases, repo settings (doctor)
  Hubra          health, alerts, errors, jobs and token of the running Hubra
                 (needs a token: `hubra login` or HUBRA_TOKEN)

Keys: ← → or 1-6 switch tabs · ↑ ↓ move · Enter or o open in browser · r refresh
· m auto-merge · M merge now · A auto-merge all ready PRs · v filter PRs
(auto-merge, recently merged, drafts) · y mark ready · d diff · b bump
· R release · S ship · a approve a waiting run · f fix drift and s sync files
(Checks tab) · t light/dark · ctrl+p every command (also setup, signing key,
Hubra login) · q quit. Links in the detail pane and the log open on click.
Every action that changes something asks first; with --dry-run it only shows what it would do.
"""

from __future__ import annotations

import contextlib
import os
import re
import time
import webbrowser
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist

from hubra_cli import cli as g

TABS = [("repos", "Repos"), ("prs", "Pull requests"), ("runs", "Runs"), ("security", "Security"),
        ("checks", "Checks"), ("hubra", "Hubra")]
CHECKS = [("lists", "Plugin lists"), ("doctor", "Repo settings"), ("verify", "Release zips")]

HUBRA_SECTIONS = [
    ("health", "Health", "/app/platform/diagnostics"),
    ("alerts", "Alerts", "/app/platform/alerts"),
    ("errors", "Errors", "/app/platform/diagnostics/errors"),
    ("jobs", "Jobs", "/app/platform/diagnostics/jobs"),
    ("whoami", "Token", "/app/platform/api-tokens"),
]

# Hubra brand: violet scale, Signal Gold, the app's dark and light neutrals.
VIOLET = {4: "#9f7cf8", 5: "#8758f0", 6: "#7441e0", 7: "#6131c4"}
GOLD = "#fece2b"
SUCCESS, WARNING, DANGER = "#1f9d57", "#e8a317", "#c73550"

THEMES = [
    Theme(name="hubra", dark=True, primary=VIOLET[4], secondary=VIOLET[5], accent=GOLD,
          foreground="#ededef", background="#111113", surface="#19191c", panel="#222226",
          success=SUCCESS, warning=WARNING, error=DANGER,
          variables={"footer-key-foreground": GOLD, "block-cursor-background": VIOLET[6],
                     "block-cursor-foreground": "#ffffff", "input-selection-background": f"{VIOLET[6]} 40%",
                     "link-color": VIOLET[4], "border": "#2f2f34"}),
    # Gold only ever sits on ink, so the light theme highlights in deep violet.
    Theme(name="hubra-light", dark=False, primary=VIOLET[6], secondary=VIOLET[7], accent=VIOLET[7],
          foreground="#1a1625", background="#f7f6fb", surface="#ffffff", panel="#ebe3ff",
          success="#18794e", warning=WARNING, error=DANGER,
          variables={"footer-key-foreground": VIOLET[7], "block-cursor-background": VIOLET[6],
                     "block-cursor-foreground": "#ffffff", "link-color": VIOLET[6], "border": "#e6e3ef"}),
]

STATE_STYLE = {
    "success": SUCCESS, "passing": SUCCESS, "ok": SUCCESS, "ready": SUCCESS,
    "failure": DANGER, "failing": DANGER, "error": DANGER, "conflicts": DANGER,
    "pending": WARNING, "running": WARNING, "waiting": WARNING, "in_progress": WARNING, "auto-merge": VIOLET[4],
}

URL = re.compile(r"https?://[^\s)\]>,'\"]+")
SHORTEN = (("https://github.com/hubra-app/", ""), ("https://github.com/", ""), ("https://", ""))


def styled(value: str) -> Text:
    for word, style in STATE_STYLE.items():
        if value.lower().startswith(word):
            return Text(value, style=style)
    return Text(value)


def link_label(url: str) -> str:
    """github.com/hubra-app/hubra-plugin-budgets/pull/12 -> hubra-plugin-budgets/pull/12."""
    for prefix, repl in SHORTEN:
        if url.startswith(prefix):
            return repl + url[len(prefix):]
    return url


def linkify(text: str, color: str = VIOLET[4]) -> Text:
    """Text with every URL as a short, clickable label (opens in the browser on click).

    The label is one unbroken word, so a long URL never ends up split over
    two lines, and clicking any part of it opens the full URL."""
    out = Text()
    pos = 0
    for m in URL.finditer(text):
        out.append(text[pos:m.start()])
        url = m.group(0).rstrip(".")
        out.append(link_label(url), style=Style(color=color, underline=True,
                                                meta={"@click": f"app.open_link({url!r})"}))
        pos = m.start() + len(url)
    out.append(text[pos:])
    return out


class Confirm(ModalScreen[bool]):
    DEFAULT_CSS = """
    Confirm { align: center middle; }
    Confirm > Vertical { width: 80; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    Confirm Horizontal { height: auto; margin-top: 1; }
    Confirm Button { margin-right: 2; }
    """
    BINDINGS: ClassVar = [Binding("y", "answer(True)", "Yes"), Binding("n,escape", "answer(False)", "No")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(Text(self.question))
            with Horizontal():
                yield Button("Yes (y)", variant="warning", id="yes")
                yield Button("No (n)", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)


class Ask(ModalScreen[str]):
    """One line of input (a path, a token); Enter answers, Esc cancels."""

    DEFAULT_CSS = """
    Ask { align: center middle; }
    Ask > Vertical { width: 80; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    Ask Input { margin-top: 1; }
    """
    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, question: str, password: bool = False, value: str = "") -> None:
        super().__init__()
        self.question, self.password, self.value = question, password, value

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Label(Text(self.question))
            yield Input(value=self.value, password=self.password)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def action_cancel(self) -> None:
        self.dismiss("")


class Pick(ModalScreen[str]):
    """Choose one of a few options; Enter picks, Esc cancels."""

    DEFAULT_CSS = """
    Pick { align: center middle; }
    Pick > Vertical { width: 80; height: auto; border: thick $accent; background: $surface; padding: 1 2; }
    Pick OptionList { height: auto; max-height: 12; margin-top: 1; }
    """
    BINDINGS: ClassVar = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, question: str, options: list[tuple[str, str]]) -> None:
        super().__init__()
        self.question, self.options = question, options

    def compose(self) -> ComposeResult:
        from textual.widgets.option_list import Option
        with Vertical():
            yield Label(Text(self.question))
            yield OptionList(*[Option(label, id=key) for key, label in self.options])

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id or "")

    def action_cancel(self) -> None:
        self.dismiss("")


PR_FILTERS = [("all", "all open"), ("auto", "auto-merge"), ("merged", "recently merged"), ("drafts", "drafts")]


class HubraTui(App):
    TITLE = "Hubra"
    CSS = """
    Screen { background: $background; }
    Header { background: $panel; color: $foreground; }
    #tabs { height: 1fr; }
    #tabs ContentSwitcher { height: 1fr; }
    Tabs .underline--bar { color: $accent; background: $panel; }
    Tab.-active { color: $accent; text-style: bold; }
    TabPane { height: 1fr; padding: 0; }
    .split { height: 1fr; }
    .split DataTable { width: 3fr; height: 1fr; background: $surface; }
    .split DataTable:focus { border-left: tall $accent; }
    DataTable > .datatable--header { background: $panel; color: $primary; text-style: bold; }
    .detail { width: 2fr; height: 1fr; border-left: solid $primary; padding: 0 1;
              overflow-x: auto; overflow-y: auto; background: $background; }
    .detail Static { width: auto; }
    #hubra-table, #checks-table { width: 22; }
    #log { height: 8; border-top: solid $primary; background: $surface; padding: 0 1;
           scrollbar-size-horizontal: 1; }
    * { scrollbar-background: $surface; scrollbar-background-hover: $surface;
        scrollbar-color: $primary 40%; scrollbar-color-hover: $primary 70%; scrollbar-color-active: $primary; }
    """
    BINDINGS: ClassVar = [
        Binding("left", "next_tab(-1)", "Prev tab", show=False, priority=True),
        Binding("right", "next_tab(1)", "Next tab", show=False, priority=True),
        Binding("1", "tab('repos')", "Repos", show=False),
        Binding("2", "tab('prs')", "PRs", show=False),
        Binding("3", "tab('runs')", "Runs", show=False),
        Binding("4", "tab('security')", "Security", show=False),
        Binding("5", "tab('checks')", "Checks", show=False),
        Binding("6", "tab('hubra')", "Hubra", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("o", "open", "Open"),
        Binding("t", "toggle_theme", "Light/dark", show=False),
        Binding("m", "merge(False)", "Auto-merge"),
        Binding("M", "merge(True)", "Merge now"),
        Binding("A", "merge_all", "Auto-merge all"),
        Binding("v", "pr_filter", "PR filter"),
        Binding("b", "bump", "Bump"),
        Binding("S", "ship", "Ship", show=False),
        Binding("y", "ready", "Ready"),
        Binding("d", "diff", "Diff"),
        Binding("R", "release", "Release"),
        Binding("a", "approve", "Approve"),
        Binding("f", "fix", "Fix drift", show=False),
        Binding("s", "sync", "Sync files", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, dry_run: bool = False) -> None:
        super().__init__()
        self.dry_run = dry_run
        self.sub_title = "dry run: nothing changes" if dry_run else "hubra-app"
        self.repos: list[str] = []
        self.prs: dict[str, dict] = {}       # "org/repo#12" -> PR node
        self.runs: dict[str, tuple] = {}     # key -> (repo, run, envs)
        self.detail_cache: dict[str, str] = {}
        self.info: dict[str, dict] = {}
        self.last_summary = ""
        self.last_waiting = 0
        self.pr_filter = "all"
        self.alerts: dict[str, tuple[list[dict], list[str]]] = {}
        self.last_alerts = 0

    # ── layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            for tab_id, title in TABS:
                with TabPane(title, id=tab_id), Horizontal(classes="split"):
                    yield DataTable(id=f"{tab_id}-table", cursor_type="row", zebra_stripes=True)
                    with VerticalScroll(classes="detail", can_focus=False):
                        yield Static(id=f"{tab_id}-detail", markup=False)
        yield RichLog(id="log", wrap=False, markup=False, min_width=40)
        yield Footer()

    def on_mount(self) -> None:
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = "hubra"
        self.query_one("#log", RichLog).can_focus = False
        heads = {
            "repos": g.STATUS_HEAD,
            "prs": ["repo", "#", "title", "state", "checks"],
            "runs": ["repo", "state", "workflow", "ref", "when"],
            "security": ["repo", "alerts", "worst", "kinds"],
            "checks": ["check"],
            "hubra": ["view"],
        }
        for tab_id, head in heads.items():
            self.query_one(f"#{tab_id}-table", DataTable).add_columns(*head)
        hubra = self.query_one("#hubra-table", DataTable)
        for key, label, _ in HUBRA_SECTIONS:
            hubra.add_row(label, key=key)
        checks = self.query_one("#checks-table", DataTable)
        for key, label in CHECKS:
            checks.add_row(label, key=key)
        self.log_line("Loading … ← → or 1-6 switch tabs, ↑ ↓ move, Enter opens, r refreshes, q quits")
        self.focus_table()
        self.load_github()
        self.load_runs()
        self.load_security()
        self.set_interval(120, self.action_refresh)

    def log_line(self, text: str) -> None:
        line = Text(time.strftime("%H:%M:%S "), style="dim")
        line.append_text(linkify(text.strip()))
        self.query_one("#log", RichLog).write(line)

    def focus_table(self, tab_id: str | None = None) -> None:
        with contextlib.suppress(Exception):
            self.query_one(f"#{tab_id or self.tab()}-table", DataTable).focus()

    def action_tab(self, tab_id: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab_id
        self.focus_table(tab_id)

    def highlight(self) -> str:
        """Signal Gold on the dark theme; deep violet on the light one (gold needs ink behind it)."""
        return VIOLET[7] if self.theme == "hubra-light" else GOLD

    def action_next_tab(self, step: int) -> None:
        if not isinstance(self.screen, ModalScreen):
            order = [t for t, _ in TABS]
            self.action_tab(order[(order.index(self.tab()) + step) % len(order)])
        else:  # in a confirm dialog the arrows move between Yes and No
            self.screen.focus_next() if step > 0 else self.screen.focus_previous()

    def action_toggle_theme(self) -> None:
        self.theme = "hubra-light" if self.theme == "hubra" else "hubra"
        self.action_refresh()

    def action_open_link(self, url: str) -> None:
        mode = g.open_mode()
        if mode == "browser":
            self.log_line(f"open {url}")
            webbrowser.open(url)
            return
        log = self.query_one("#log", RichLog)
        line = Text(time.strftime("%H:%M:%S "), style="dim")
        if mode == "copy":
            # OSC 52: the terminal on your own machine puts it on the clipboard, also over SSH
            self.copy_to_clipboard(url)
            line.append("copied ")
        line.append(url, style=Style(underline=True, link=url))  # full URL, to select or cmd/ctrl-click
        log.write(line)

    def tab(self) -> str:
        try:
            return self.query_one("#tabs", TabbedContent).active
        except NoMatches:  # the app is closing
            return ""

    def current_key(self, tab_id: str | None = None) -> str | None:
        tab_id = tab_id or self.tab()
        if not tab_id:
            return None
        table = self.query_one(f"#{tab_id}-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except (KeyError, IndexError, ValueError, CellDoesNotExist):
            return None

    def show(self, tab_id: str, text: str) -> None:
        self.query_one(f"#{tab_id}-detail", Static).update(linkify(text))

    # ── loading ───────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True, group="github")
    def load_github(self) -> None:
        g._repos_cache.clear()
        try:
            names = g.plugin_names()
            self.repos = [g.full(c) for c in g.CORE] + [g.full(n) for n in names]
            rows = g.status_rows(self.repos)
            info = g.org_repos(full_info=True)
        except RuntimeError as err:
            self.call_from_thread(self.log_line, f"GitHub: {err}")
            return
        prs = {}
        for repo in self.repos:
            for p in ((info.get(g.short(repo)) or {}).get("pullRequests") or {}).get("nodes") or []:
                prs[f"{repo}#{p['number']}"] = dict(p, repo=repo)
        self.call_from_thread(self.fill_github, rows, prs, info)

    def fill_github(self, rows: list[list[str]], prs: dict[str, dict], info: dict) -> None:
        with contextlib.suppress(NoMatches):  # the app is closing
            self._fill_github(rows, prs, info)

    def _fill_github(self, rows: list[list[str]], prs: dict[str, dict], info: dict) -> None:
        self.prs, self.info, self.detail_cache = prs, info, {}
        table = self.query_one("#repos-table", DataTable)
        keep = self.current_key("repos")
        table.clear()
        for repo, row in zip(self.repos, rows):
            cells = [Text(c) for c in row]
            cells[3] = Text(row[3], style=f"bold {self.highlight()}") if row[3] else Text("")
            cells[5] = styled(row[5])
            table.add_row(*cells, key=repo)
        self.restore(table, keep)

        for repo in self.repos:
            for p in ((info.get(g.short(repo)) or {}).get("merged") or {}).get("nodes") or []:
                self.prs[f"{repo}#{p['number']}"] = dict(p, repo=repo, merged=True, isDraft=False)
        self.fill_prs()
        todo = sum(1 for r in rows if r[3])
        summary = f"{len(self.repos)} repos, {len(prs)} open PRs, {todo} plugin(s) to release"
        auto = sum(1 for p in prs.values() if p.get("autoMergeRequest"))
        if auto:
            summary += f", {auto} auto-merging (v filters the PR list)"
        if summary != self.last_summary:
            self.last_summary = summary
            self.log_line(summary)
        self.update_detail()

    def fill_prs(self) -> None:
        table = self.query_one("#prs-table", DataTable)
        keep = self.current_key("prs")
        table.clear()
        rows = []
        for key, p in self.prs.items():
            if p.get("merged"):
                state, checks, sort = Text(f"merged {g.ago(p.get('mergedAt'))}", style=SUCCESS), Text(""), 3
            else:
                commit = (p["commits"]["nodes"] or [{}])[0].get("commit") or {}
                word = g.checks_word(commit.get("statusCheckRollup"))
                checks, sort = styled(word), 1
                if p.get("mergeable") == "CONFLICTING":
                    state = styled("conflicts")
                elif p.get("autoMergeRequest"):
                    # what auto-merge waits for: checks running, or blocked by failing checks
                    state = Text(f"auto-merge · {'blocked' if word == 'failing' else 'waits for checks'}",
                                 style=DANGER if word == "failing" else VIOLET[4])
                    sort = 0
                else:
                    state = styled("draft" if p["isDraft"] else "ready")
                    sort = 2 if p["isDraft"] else 1
            keep_row = {"all": not p.get("merged"), "auto": bool(p.get("autoMergeRequest")) or p.get("merged"),
                        "merged": p.get("merged"), "drafts": p.get("isDraft")}[self.pr_filter]
            if keep_row:
                rows.append((sort, key, [g.short(p["repo"]).removeprefix(g.PREFIX), str(p["number"]),
                                         Text(p["title"][:60]), state, checks]))
        for _, key, cells in sorted(rows, key=lambda r: r[0]):
            table.add_row(*cells, key=key)
        self.restore(table, keep)
        label = dict(PR_FILTERS)[self.pr_filter]
        auto = sum(1 for p in self.prs.values() if p.get("autoMergeRequest"))
        with contextlib.suppress(NoMatches):
            self.query_one("#tabs", TabbedContent).get_tab("prs").label = (
                "Pull requests" + (f" ({label})" if self.pr_filter != "all" else "")
                + (f" · {auto} auto-merging" if auto else ""))

    @work(thread=True, exclusive=True, group="security")
    def load_security(self) -> None:
        try:
            repos = self.repos or [g.full(c) for c in g.CORE] + [g.full(n) for n in g.plugin_names()]
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(g.WORKERS) as pool:
                found = dict(zip(repos, pool.map(g.repo_alerts, repos)))
        except RuntimeError as err:
            self.call_from_thread(self.log_line, f"Security: {err}")
            return
        self.call_from_thread(self.fill_security, found)

    def fill_security(self, found: dict) -> None:
        with contextlib.suppress(NoMatches):
            self.alerts = found
            table = self.query_one("#security-table", DataTable)
            keep = self.current_key("security")
            table.clear()
            rank = g.SEVERITY_ORDER
            rows = sorted(found.items(), key=lambda kv: (rank.get(kv[1][0][0]["severity"], 5) if kv[1][0] else 9,
                                                          -len(kv[1][0]), kv[0]))
            for repo, (alerts, unread) in rows:
                worst = alerts[0]["severity"] if alerts else ("?" if unread else "-")
                style = DANGER if worst in ("critical", "high", "secret") else WARNING if alerts else ""
                kinds = ", ".join(sorted({a["kind"] for a in alerts})) or ("can't read all" if unread else "")
                table.add_row(g.short(repo).removeprefix(g.PREFIX), Text(str(len(alerts)), style=style),
                              Text(worst, style=style), kinds, key=repo)
            self.restore(table, keep)
            total = sum(len(a) for a, _ in found.values())
            self.query_one("#tabs", TabbedContent).get_tab("security").label = (
                f"Security ({total})" if total else "Security")
            if total and total != self.last_alerts:
                self.log_line(f"{total} open security alert(s): Security tab (4)")
            self.last_alerts = total
            self.update_detail()

    def security_text(self, repo: str) -> str:
        alerts, unread = self.alerts.get(repo, ([], []))
        lines = [repo, f"{g.WEB}/{repo}/security", ""]
        for a in alerts:
            lines.append(f"{a['severity']:<9} {a['kind']:<16} {a['title']}")
            if a["where"]:
                lines.append(f"{'':<26} {a['where']}")
            lines.append(f"{'':<26} {a['url']}")
        if not alerts:
            lines.append("No open alerts.")
        if unread:
            lines += ["", "Couldn't read:"] + [f"  {u}" for u in unread]
        return "\n".join(lines)

    def action_pr_filter(self) -> None:
        order = [k for k, _ in PR_FILTERS]
        self.pr_filter = order[(order.index(self.pr_filter) + 1) % len(order)]
        self.action_tab("prs")
        self.fill_prs()
        self.log_line(f"Pull requests: {dict(PR_FILTERS)[self.pr_filter]} (v switches)")

    @work(thread=True, exclusive=True, group="runs")
    def load_runs(self) -> None:
        try:
            repos = self.repos or [g.full(c) for c in g.CORE] + [g.full(n) for n in g.plugin_names()]
            waiting = g.all_waiting(repos)

            def latest(repo):
                runs = (g.api("GET", f"/repos/{repo}/actions/runs?per_page=1") or {}).get("workflow_runs") or []
                return repo, runs[0] if runs else None

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(g.WORKERS) as pool:
                last = list(pool.map(latest, repos))
        except RuntimeError as err:
            self.call_from_thread(self.log_line, f"Runs: {err}")
            return
        self.call_from_thread(self.fill_runs, waiting, last)

    def fill_runs(self, waiting, last) -> None:
        with contextlib.suppress(NoMatches):  # the app is closing
            self._fill_runs(waiting, last)

    def _fill_runs(self, waiting, last) -> None:
        table = self.query_one("#runs-table", DataTable)
        keep = self.current_key("runs")
        table.clear()
        self.runs = {}
        seen = set()
        for repo, run, envs in waiting:
            key = f"{repo}|{run['id']}"
            seen.add(run["id"])
            self.runs[key] = (repo, run, envs)
            waiting_text = Text("waiting for you", style=f"bold {self.highlight()}")
            table.add_row(g.short(repo).removeprefix(g.PREFIX), waiting_text,
                          run.get("name") or "", run.get("head_branch") or "", g.ago(run.get("created_at")), key=key)
        for repo, run in last:
            if not run or run["id"] in seen:
                continue
            key = f"{repo}|{run['id']}"
            self.runs[key] = (repo, run, [])
            state = "waiting" if run.get("status") == "waiting" else (run.get("conclusion") or run.get("status") or "?")
            table.add_row(g.short(repo).removeprefix(g.PREFIX), styled(state), run.get("name") or "",
                          run.get("head_branch") or "", g.ago(run.get("created_at")), key=key)
        self.restore(table, keep)
        if len(waiting) != self.last_waiting and waiting:
            self.log_line(f"{len(waiting)} run(s) wait for your approval: Runs tab (3), key a")
        self.last_waiting = len(waiting)
        base = "dry run: nothing changes" if self.dry_run else "hubra-app"
        self.sub_title = f"{base} · {len(waiting)} to approve (3, a)" if waiting else base
        with contextlib.suppress(NoMatches):
            tabs = self.query_one("#tabs", TabbedContent)
            tabs.get_tab("runs").label = f"Runs ({len(waiting)} to approve)" if waiting else "Runs"
        self.update_detail()

    @staticmethod
    def restore(table: DataTable, key: str | None) -> None:
        if key is None:
            return
        with contextlib.suppress(RowDoesNotExist):
            table.move_cursor(row=table.get_row_index(key))

    # ── detail pane ───────────────────────────────────────────────────────

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self.update_detail()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.focus_table()
        self.update_detail()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.action_open()

    def update_detail(self) -> None:
        tab, key = self.tab(), self.current_key()
        if not key:
            return
        if tab == "repos":
            self.show("repos", self.repo_text(key))
        elif tab == "prs":
            if key in self.detail_cache:
                self.show("prs", self.detail_cache[key])
            else:
                self.show("prs", "Loading …")
                self.load_pr(key)
        elif tab == "security":
            self.show("security", self.security_text(key))
        elif tab == "runs":
            repo, run, envs = self.runs[key]
            lines = [f"{g.short(repo)}  {run.get('name')}", run["html_url"], "",
                     f"event    {run.get('event')}", f"ref      {run.get('head_branch')}",
                     f"state    {g.run_state(run)}", f"started  {g.ago(run.get('created_at'))}"]
            if envs:
                lines += ["", f"Waits for: {', '.join(e['name'] for e in envs)}", "a approves it"]
            self.show("runs", "\n".join(lines))
        elif tab in ("hubra", "checks"):
            cache_key = f"{tab}:{key}"
            if cache_key in self.detail_cache:
                self.show(tab, self.detail_cache[cache_key])
            else:
                self.show(tab, "Checking …" if tab == "checks" else "Loading …")
                self.load_view(tab, key)

    def repo_text(self, repo: str) -> str:
        node = self.info.get(g.short(repo)) or {}
        m = g.parse_manifest((node.get("manifest") or {}).get("text")) if g.is_plugin(repo) else None
        tags = [r["tagName"] for r in (node.get("releases") or {}).get("nodes") or []]
        last = g.newest(tags)
        lines = [repo, f"{g.WEB}/{repo}", ""]
        if m:
            lines.append(f"plugin.yaml  {m[0]} {m[1]}")
        lines.append(f"released     {last or '-'}" + (f"  {g.WEB}/{repo}/releases/tag/{last}" if last else ""))
        if m and m[0] != "example" and (not last or g.version_tuple(m[1]) > g.version_tuple(last)):
            lines.append(f"to release   v{m[1]}  (R releases it)")
        lines += ["", "Open PRs:"]
        for p in sorted(((node.get("pullRequests") or {}).get("nodes") or []), key=lambda p: p["number"]):
            lines.append(f"  #{p['number']} {p['title']}{' (draft)' if p['isDraft'] else ''}")
            lines.append(f"     {p['url']}")
        if len(lines) and lines[-1] == "Open PRs:":
            lines.append("  none")
        lines += ["", f"Actions  {g.WEB}/{repo}/actions"]
        return "\n".join(lines)

    @work(thread=True, group="detail")
    def load_pr(self, key: str, diff: bool = False) -> None:
        repo, number = key.rsplit("#", 1)
        text = g.capture(g.pr_detail, repo, int(number), diff, False, False)
        if not diff:
            self.detail_cache[key] = text
        self.call_from_thread(self.show_if_current, "prs", key, text)

    @work(thread=True, group="detail")
    def load_view(self, tab: str, key: str) -> None:
        if tab == "hubra":
            text = g.capture(g.HUBRA_VIEWS[key])
        elif key == "lists":
            text = g.capture(g.lists, False)
        elif key == "verify":
            text = g.capture(g.for_each, [r for r in self.repos if g.is_plugin(r)], g.verify, {})
        else:
            text = g.capture(g.doctor_all, self.repos)
            text += "\nf applies the fixes (doctor --fix) · s opens PRs with the template's files (sync)\n"
        self.detail_cache[f"{tab}:{key}"] = text
        self.call_from_thread(self.show_if_current, tab, key, text)

    def show_if_current(self, tab: str, key: str, text: str) -> None:
        if self.tab() == tab and self.current_key(tab) == key:
            self.show(tab, text)

    # ── actions ───────────────────────────────────────────────────────────

    def action_refresh(self) -> None:
        self.detail_cache.clear()
        self.load_github()
        self.load_runs()
        self.load_security()
        if self.tab() in ("hubra", "checks"):
            self.update_detail()

    def action_open(self) -> None:
        tab, key = self.tab(), self.current_key()
        if not key:
            return
        if tab == "repos":
            url = f"{g.WEB}/{key}"
        elif tab == "prs":
            url = self.prs[key]["url"]
        elif tab == "runs":
            url = self.runs[key][1]["html_url"]
        elif tab == "security":
            alerts = self.alerts.get(key, ([], []))[0]
            url = alerts[0]["url"] if len(alerts) == 1 else f"{g.WEB}/{key}/security"
        elif tab == "checks":
            docs = f"{g.WEB}/{g.ORG}/hubra/blob/main/docs/plugin-releases.md"
            url = docs if key == "lists" else f"{g.WEB}/{g.TEMPLATE}"
        else:
            url = g.MANAGE + {k: path for k, _, path in HUBRA_SECTIONS}[key]
        self.action_open_link(url)

    def action_fix(self) -> None:
        if self.tab() == "checks" and self.current_key() == "lists":
            self.confirm_then("Open PRs for outdated plugin versions in the hubra README and the\n"
                              "Marketplace README? (hubra lists --fix)", g.lists, True)
            return
        if self.tab() != "checks" or self.current_key() != "doctor":
            self.log_line("f fixes drift from the Checks tab › Repo settings or Plugin lists")
            return
        self.confirm_then("Apply the template's settings and rulesets (and each repo's own rulesets files)\n"
                          "to every repo that drifted? (hubra doctor --fix)", g.doctor_all, list(self.repos), True)

    def action_sync(self) -> None:
        if self.tab() != "checks" or self.current_key() != "doctor":
            self.log_line("s syncs files from the Checks tab › Repo settings")
            return
        plugins = [r for r in self.repos if g.is_plugin(r)]

        def sync_all(repos) -> None:
            g.for_each(repos, g.sync, g.sync_ctx())
        self.confirm_then("Open a PR in each plugin whose shared files (release.yml, rulesets, SECURITY.md, ...)\n"
                          "differ from the template? (hubra sync)", sync_all, plugins)

    def selected_plugin(self, verb: str) -> str | None:
        key = self.current_key()
        if self.tab() == "prs" and key:
            key = self.prs[key]["repo"]
        if self.tab() not in ("repos", "prs") or not key or not g.is_plugin(key):
            self.log_line(f"{verb}: pick a plugin in the Repos tab (1) first")
            return None
        return key

    def action_bump(self) -> None:
        repo = self.selected_plugin("b bumps a plugin's version")
        if not repo:
            return
        name = g.short(repo).removeprefix(g.PREFIX)
        options = [(f"{lvl}{'+auto' if auto else ''}", f"{lvl}{' and auto-merge the PR' if auto else ''}")
                   for auto in (False, True) for lvl in ("patch", "minor", "major")]

        def chosen(choice: str | None) -> None:
            if choice:
                level, _, auto = choice.partition("+")
                self.log_line(f"bump {name} {level}{' (auto-merge)' if auto else ''} …")
                self.do_job(g.for_each, [repo], g.bump, {"level": level, "auto": bool(auto)}, False)
        self.push_screen(Pick(f"Bump {name}: open a PR with the next version in plugin.yaml and a\n"
                              "CHANGELOG section of the PRs merged since the last release.", options), chosen)

    def action_ship(self) -> None:
        repo = self.selected_plugin("S ships a plugin")
        if repo:
            self.confirm_then(f"Ship {g.short(repo)}: merge its ready PRs, then release the version in\n"
                              "plugin.yaml (pushes the tag; the workflow builds the release)?",
                              g.for_each, [repo], g.ship, {"pick": {}, "now": True, "ready": False}, False)

    def action_setup(self) -> None:
        repo = self.selected_plugin("setup")
        if repo:
            def setup(repo: str) -> None:
                g.for_each([repo], g.setup, {"me": g.api("GET", "/user"), "template": g.template_baseline()}, False)
            self.confirm_then(f"Set up {g.short(repo)}: copy the template's settings and rulesets and\n"
                              "create the plugin-signing environment (you as reviewer, v* tags)?", setup, repo)

    def action_store_key(self) -> None:
        repo = self.selected_plugin("key")
        if not repo:
            return

        def got(path: str | None) -> None:
            if not path:
                return
            try:
                key = g.read_key(os.path.expanduser(path))
            except SystemExit as err:  # read_key exits with the reason
                self.log_line(str(err))
                return
            self.confirm_then(f"Store this key as HUBRA_SIGNING_KEY in {g.short(repo)}'s plugin-signing\n"
                              "environment?", g.set_key, repo, key)
        self.push_screen(Ask("Private key file of Hubra.pub (PEM), e.g. ~/keys/hubra-plugin.key"), got)

    def action_login(self) -> None:
        def got(token: str | None) -> None:
            if token:
                self.do_job(lambda t: g.say(g.save_token(t)), token)
        self.push_screen(Ask("Hubra API token (hbr_…), kept in ~/.config/hubra/token", password=True), got)

    def get_system_commands(self, screen):
        yield from super().get_system_commands(screen)
        commands = [
            ("Auto-merge all ready PRs", "A · every ready PR merges once its checks pass", self.action_merge_all),
            ("Filter pull requests", "v · all, auto-merge, recently merged, drafts", self.action_pr_filter),
            ("Bump plugin version", "b · PR with the next version and changelog", self.action_bump),
            ("Release plugin", "R · push the tag for the version in plugin.yaml", self.action_release),
            ("Ship plugin", "S · merge its ready PRs, then release", self.action_ship),
            ("Approve waiting run", "a · signing or an outside contributor's run", self.action_approve),
            ("Fix drift", "f · hubra doctor --fix / lists --fix (Checks tab)", self.action_fix),
            ("Sync template files", "s · PRs with the template's shared files", self.action_sync),
            ("Set up plugin repo", "copy the template, create the plugin-signing environment", self.action_setup),
            ("Store signing key", "HUBRA_SIGNING_KEY for the selected plugin", self.action_store_key),
            ("Hubra login", "save a Hubra API token for the Hubra tab", self.action_login),
            ("Security alerts", "4 · open Dependabot, secret and code scanning alerts",
             lambda: self.action_tab("security")),
            ("Verify release zips", "Checks › Release zips", lambda: self.show_check("verify")),
            ("Check plugin lists", "Checks › Plugin lists", lambda: self.show_check("lists")),
            ("Check repo settings", "Checks › Repo settings (doctor)", lambda: self.show_check("doctor")),
            ("Light/dark theme", "t", self.action_toggle_theme),
        ]
        for title, help_text, callback in commands:
            yield SystemCommand(title, help_text, callback)

    def show_check(self, key: str) -> None:
        self.action_tab("checks")
        self.restore(self.query_one("#checks-table", DataTable), key)

    def confirm_then(self, question: str, job, *args) -> None:
        if self.dry_run:
            question += "\n\n(dry run: only shows what it would do)"

        def done(yes: bool | None) -> None:
            if yes:
                self.do_job(job, *args)
        self.push_screen(Confirm(question), done)

    @work(thread=True, group="action")
    def do_job(self, job, *args) -> None:
        text = g.capture(job, *args)
        self.call_from_thread(self.after_action, text)

    def after_action(self, text: str) -> None:
        if not self.tab():
            return
        for line in text.splitlines():
            if line.strip():
                self.log_line(line)
        self.action_refresh()

    def action_merge(self, now: bool) -> None:
        tab, key = self.tab(), self.current_key()
        if not key or tab not in ("repos", "prs"):
            return
        if tab == "prs":
            p = self.prs[key]
            if p.get("merged"):
                self.log_line(f"{g.short(p['repo'])}#{p['number']} is already merged")
                return
            repo = p["repo"]
            ctx = {"pick": {repo: [p["number"]]}, "now": now, "ready": False}
            what = f"{'Merge now' if now else 'Turn on auto-merge for'} {g.short(repo)}#{p['number']}\n{p['title']}?"
        else:
            repo = key
            ctx = {"pick": {}, "now": now, "ready": False}
            what = f"{'Merge now' if now else 'Auto-merge'} every ready PR in {g.short(repo)}?"
        self.confirm_then(what, g.merge, repo, ctx)

    def action_merge_all(self) -> None:
        ready = [p for p in self.prs.values() if not p["isDraft"] and not p.get("merged")
                 and p.get("mergeable") != "CONFLICTING" and not p.get("autoMergeRequest")]
        if not ready:
            self.log_line("No ready PRs without auto-merge (drafts and conflicts are left alone)")
            return
        pick: dict[str, list[int]] = {}
        for p in ready:
            pick.setdefault(p["repo"], []).append(p["number"])
        names = "\n".join(f"  {g.short(p['repo']).removeprefix(g.PREFIX)}#{p['number']} {p['title'][:60]}"
                          for p in ready[:15]) + (f"\n  … and {len(ready) - 15} more" if len(ready) > 15 else "")

        def merge_all(pick: dict[str, list[int]]) -> None:
            g.for_each(list(pick), g.merge, {"pick": pick, "now": False, "ready": False})
        self.confirm_then(f"Turn on auto-merge for {len(ready)} ready PR(s)? Each merges once its checks pass.\n\n"
                          f"{names}", merge_all, pick)

    def action_ready(self) -> None:
        key = self.current_key()
        if self.tab() != "prs" or not key:
            self.log_line("y marks a draft PR ready: pick one in the Pull requests tab (2)")
            return
        p = self.prs[key]
        if not p["isDraft"]:
            done = "merged" if p.get("merged") else "ready for review"
            self.log_line(f"{g.short(p['repo'])}#{p['number']} is already {done}")
            return

        def ready(repo: str, number: int) -> None:
            if g.DRY_RUN:
                g.say(f"[dry-run] mark {g.short(repo)}#{number} ready for review")
            else:
                g.gh("pr", "ready", str(number), "--repo", repo)
                g.say(f"{g.short(repo)}#{number} is ready for review")
        self.confirm_then(f"Mark {g.short(p['repo'])}#{p['number']} ready for review?", ready, p["repo"], p["number"])

    def action_diff(self) -> None:
        key = self.current_key()
        if self.tab() == "prs" and key:
            self.show("prs", "Loading diff …")
            self.load_pr(key, diff=True)

    def action_release(self) -> None:
        key = self.current_key()
        if self.tab() != "repos" or not key:
            return
        if not g.is_plugin(key):
            self.log_line("Core releases follow docs/release-guidelines.md")
            return
        self.confirm_then(f"Release {g.short(key)} at the version in plugin.yaml?\n"
                          "(pushes the tag v<version> if it is new; the workflow builds the release)",
                          g.release, key, {})

    def action_approve(self) -> None:
        key = self.current_key()
        if self.tab() != "runs" or not key:
            self.log_line("a approves a run waiting for you: Runs tab (3)")
            return
        repo, run, envs = self.runs[key]
        if not envs:
            self.log_line("That run isn't waiting for your approval")
            return

        def approve(repo, run, envs) -> None:
            if g.DRY_RUN:
                g.say(f"[dry-run] approve {g.short(repo)} {run.get('head_branch')}")
            else:
                g.approve_run(repo, run, envs)
                g.say(f"approved {', '.join(e['name'] for e in envs)} for {g.short(repo)} {run.get('head_branch')}")
        if g.RUN_APPROVAL in envs:
            question = (f"Let {g.short(repo)} run {run.get('name')} on {run.get('head_branch')}?\n"
                        "It comes from an outside contributor: check the PR's changes first.\n"
                        f"{run.get('html_url')}")
        else:
            question = (f"Approve {', '.join(e['name'] for e in envs)} for {g.short(repo)} "
                        f"{run.get('head_branch')}?\nThis signs the plugin with the official key.")
        self.confirm_then(question, approve, repo, run, envs)


def run(dry_run: bool = False) -> None:
    g.DRY_RUN = dry_run
    HubraTui(dry_run=dry_run).run()


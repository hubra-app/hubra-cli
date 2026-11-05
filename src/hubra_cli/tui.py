"""hubra tui: one full-screen view of the hubra-app repos and the running Hubra.

Start it with `hubra tui`; it needs the [tui] extra (README › Install) or
`pipx inject hubra-cli textual`. Tabs:

  Repos          version on main, latest release, what's left to release, PRs, CI
  Pull requests  every open PR; the right side shows the selected one in depth
  Runs           signing runs waiting for you, then the newest run per repo
  Checks         plugin lists against the releases, repo settings (doctor)
  Hubra          health, alerts, errors, jobs and token of the running Hubra
                 (needs a token: `hubra login` or HUBRA_TOKEN)

Keys: 1-5 switch tabs · arrows move · Enter or o open in browser · r refresh
· m auto-merge · M merge now · y mark ready · d diff · R release · a approve
signing · f fix drift and s sync files (Checks › Repo settings) · t light/dark
· q quit. Links in the detail pane and the log open on
click. Every action that changes something asks first; with --dry-run it only
shows what it would do.
"""

from __future__ import annotations

import contextlib
import re
import time
import webbrowser
from typing import ClassVar

from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
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
    Label,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.data_table import CellDoesNotExist, RowDoesNotExist

from hubra_cli import cli as g

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
        Binding("1", "tab('repos')", "Repos", show=False),
        Binding("2", "tab('prs')", "PRs", show=False),
        Binding("3", "tab('runs')", "Runs", show=False),
        Binding("4", "tab('checks')", "Checks", show=False),
        Binding("5", "tab('hubra')", "Hubra", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("o", "open", "Open"),
        Binding("t", "toggle_theme", "Light/dark", show=False),
        Binding("m", "merge(False)", "Auto-merge"),
        Binding("M", "merge(True)", "Merge now"),
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

    # ── layout ────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with TabbedContent(id="tabs"):
            for tab_id, title in (("repos", "Repos"), ("prs", "Pull requests"),
                                  ("runs", "Runs"), ("checks", "Checks"), ("hubra", "Hubra")):
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
        self.log_line("Loading … 1-5 switch tabs, arrows move, Enter opens, r refreshes, q quits")
        self.focus_table()
        self.load_github()
        self.load_runs()
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

    def action_toggle_theme(self) -> None:
        self.theme = "hubra-light" if self.theme == "hubra" else "hubra"
        self.action_refresh()

    def action_open_link(self, url: str) -> None:
        self.log_line(f"open {url}")
        webbrowser.open(url)

    def tab(self) -> str:
        return self.query_one("#tabs", TabbedContent).active

    def current_key(self, tab_id: str | None = None) -> str | None:
        table = self.query_one(f"#{tab_id or self.tab()}-table", DataTable)
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

        table = self.query_one("#prs-table", DataTable)
        keep = self.current_key("prs")
        table.clear()
        for key, p in prs.items():
            commit = (p["commits"]["nodes"] or [{}])[0].get("commit") or {}
            state = "draft" if p["isDraft"] else "ready"
            if p.get("mergeable") == "CONFLICTING":
                state = "conflicts"
            elif p.get("autoMergeRequest"):
                state = "auto-merge"
            table.add_row(g.short(p["repo"]).removeprefix(g.PREFIX), str(p["number"]),
                          Text(p["title"][:60]), styled(state),
                          styled(g.checks_word(commit.get("statusCheckRollup"))), key=key)
        self.restore(table, keep)
        todo = sum(1 for r in rows if r[3])
        summary = f"{len(self.repos)} repos, {len(prs)} open PRs, {todo} plugin(s) to release"
        if summary != self.last_summary:
            self.last_summary = summary
            self.log_line(summary)
        self.update_detail()

    @work(thread=True, exclusive=True, group="runs")
    def load_runs(self) -> None:
        try:
            plugins = [r for r in self.repos or [g.full(n) for n in g.plugin_names()] if g.is_plugin(r)]
            waiting = g.all_waiting(plugins)
            repos = self.repos or [g.full(c) for c in g.CORE] + plugins

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
            self.log_line(f"{len(waiting)} run(s) wait for your signing approval: Runs tab, key a")
        self.last_waiting = len(waiting)
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
        elif tab == "checks":
            docs = f"{g.WEB}/{g.ORG}/hubra/blob/main/docs/plugin-releases.md"
            url = docs if key == "lists" else f"{g.WEB}/{g.TEMPLATE}"
        else:
            url = g.MANAGE + {k: path for k, _, path in HUBRA_SECTIONS}[key]
        self.action_open_link(url)

    def action_fix(self) -> None:
        if self.tab() != "checks" or self.current_key() != "doctor":
            self.log_line("f fixes drift from the Checks tab › Repo settings")
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
            repo = p["repo"]
            ctx = {"pick": {repo: [p["number"]]}, "now": now, "ready": False}
            what = f"{'Merge now' if now else 'Turn on auto-merge for'} {g.short(repo)}#{p['number']}\n{p['title']}?"
        else:
            repo = key
            ctx = {"pick": {}, "now": now, "ready": False}
            what = f"{'Merge now' if now else 'Auto-merge'} every ready PR in {g.short(repo)}?"
        self.confirm_then(what, g.merge, repo, ctx)

    def action_ready(self) -> None:
        key = self.current_key()
        if self.tab() != "prs" or not key or not self.prs[key]["isDraft"]:
            return
        p = self.prs[key]

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
        self.confirm_then(f"Approve signing of {g.short(repo)} {run.get('head_branch')}?\n"
                          f"This signs the plugin with the official key.", approve, repo, run, envs)


def run(dry_run: bool = False) -> None:
    g.DRY_RUN = dry_run
    HubraTui(dry_run=dry_run).run()


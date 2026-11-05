"""hubra as a bitsandbricks (bnb) plugin.

With bitsandbricks-cli installed next to hubra-cli (`pipx inject bitsandbricks-cli hubra-cli`),
bnb gets the Hubra parts:

  commands     bnb ship | verify | key | setup | lists | hubra | login | logout
               (the same as the hubra command; bnb itself does bump, release, status, prs, merge,
               doctor, sync, ... with the version files bitsandbricks.toml names)
  status       version (plugin.yaml, or app/backend/pyproject.toml for hubra) and what is left to release
  doctor       the plugin-signing environment, HUBRA_SIGNING_KEY, release.yml
  TUI          Hubra tab (health, alerts, errors, jobs, token), Checks › Plugin lists and
               Release zips, key S ship (plugins), the Hubra themes. b and R (bump, release) are bnb's own.

The class doesn't import bitsandbricks: bnb only needs these methods, so hubra-cli
works (and tests) without bnb installed.
"""

from __future__ import annotations

import argparse
import contextlib

from hubra_cli import cli

COMMANDS = {
    "ship": "merge a plugin's ready PRs, then release it",
    "verify": "check release zips against Hubra.pub",
    "key": "store HUBRA_SIGNING_KEY in plugin-signing",
    "setup": "create or set up a plugin repo from the template",
    "lists": "plugin lists against the releases",
    "hubra": "the running Hubra: health, alerts, errors, jobs, whoami, get",
    "login": "save a Hubra API token",
    "logout": "forget the saved Hubra API token",
}
SECTIONS = [  # Hubra tab: key, label, page in the manage app
    ("health", "Health", "/app/platform/diagnostics"),
    ("alerts", "Alerts", "/app/platform/alerts"),
    ("errors", "Errors", "/app/platform/diagnostics/errors"),
    ("jobs", "Jobs", "/app/platform/diagnostics/jobs"),
    ("whoami", "Token", "/app/platform/api-tokens"),
]


def is_hubra_plugin(repo: str) -> bool:
    return repo.startswith(f"{cli.ORG}/{cli.PREFIX}")


def manifest_of(node: dict) -> tuple[str, str] | None:
    return cli.parse_manifest((node.get("manifest") or {}).get("text"))


def has_version(repo: str) -> bool:
    return is_hubra_plugin(repo) or repo in cli.CORE_VERSION


class HubraPlugin:
    name = "hubra"
    description = "Hubra plugin releases, signing, plugin lists and the Hubra API"
    graphql_fields = ('manifest: object(expression: "HEAD:plugin.yaml") { ... on Blob { text } }\n'
                      'pyproject: object(expression: "HEAD:app/backend/pyproject.toml") { ... on Blob { text } }')

    # ── commands ──────────────────────────────────────────────────────────

    def add_commands(self, sub) -> dict:
        handlers = {}
        for cmd, help_text in COMMANDS.items():
            sp = sub.add_parser(cmd, help=f"{help_text} [hubra]", add_help=False)
            sp.add_argument("rest", nargs=argparse.REMAINDER)
            handlers[cmd] = self.runner(cmd)
        return handlers

    @staticmethod
    def runner(cmd: str):
        def run(args) -> None:
            cli.main([cmd, *getattr(args, "_extra", []), *args.rest])
        return run

    # ── status and doctor ─────────────────────────────────────────────────

    def status_columns(self) -> list:
        def version(repo: str, node: dict) -> str:
            m = manifest_of(node) if is_hubra_plugin(repo) else None
            if m and m[0] == "example":
                return "template"
            return cli.node_version(repo, node) or ""
        return [("version", version), ("to release", cli.to_release)]

    def repo_detail(self, repo: str, node: dict) -> list[str]:
        if not has_version(repo):
            return []
        m = manifest_of(node) if is_hubra_plugin(repo) else None
        if m:
            lines = [f"plugin.yaml  {m[0]} {m[1]}"]
        else:
            lines = [f"version      {cli.node_version(repo, node) or '-'}  ({cli.CORE_VERSION[repo][0]})"]
        todo = cli.to_release(repo, node)
        if todo:
            lines.append(f"to release   {todo}  (R pushes the tag, b bumps the version)")
        return lines

    def doctor(self, repo: str, group, ctx: dict) -> list[str]:
        if not is_hubra_plugin(repo):
            return []
        return cli.signing_problems(repo, None)  # release.yml drift: bnb sync covers the template's files

    def doctor_hints(self, repo: str, group, problems: list[str]) -> list[str]:
        if not is_hubra_plugin(repo):
            return []
        return cli.signing_hints(cli.short(repo).removeprefix(cli.PREFIX), problems, cmd="bnb")

    # ── TUI ───────────────────────────────────────────────────────────────

    def tui_tabs(self) -> list[dict]:
        def text(key: str) -> str:
            return cli.capture(cli.HUBRA_VIEWS[key])
        pages = {k: path for k, _, path in SECTIONS}
        return [{"id": "hubra", "title": "Hubra", "rows": [(k, label) for k, label, _ in SECTIONS],
                 "text": text, "url": lambda key: cli.MANAGE + pages[key]}]

    def tui_checks(self) -> list:
        def lists(repos: list[str]) -> str:
            return cli.capture(cli.lists, False)

        def verify(repos: list[str]) -> str:
            return cli.capture(cli.for_each, [r for r in repos if is_hubra_plugin(r)], cli.verify, {})
        return [("hubra-lists", "Plugin lists", lists), ("hubra-verify", "Release zips", verify)]

    def tui_keys(self) -> list:
        return [("S", "Ship", self.ship)]

    def tui_commands(self) -> list:
        return [("Ship plugin", "S · merge its ready PRs, then release", self.ship),
                ("Hubra login", "save a Hubra API token for the Hubra tab", self.login)]

    def tui_themes(self) -> list:
        with contextlib.suppress(ImportError):
            from hubra_cli.tui import THEMES
            return list(THEMES)
        return []

    @staticmethod
    def plugin_repo(app, verb: str) -> str | None:
        repo = app.selected_repo()
        if not repo or not has_version(repo):
            app.log_line(f"{verb}: pick a Hubra plugin or hubra in the Repos tab first")
            return None
        return repo

    def ship(self, app) -> None:
        repo = self.plugin_repo(app, "S ships a plugin")
        if repo:
            app.confirm_then(f"Ship {cli.short(repo)}: merge its ready PRs, then release the version in\n"
                             "plugin.yaml (pushes the tag; the workflow builds the release)?",
                             self.with_dry_run(app, cli.for_each), [repo], cli.ship,
                             {"pick": {}, "now": True, "ready": False}, False)

    def login(self, app) -> None:
        def got(token: str | None) -> None:
            if token:
                app.do_job(lambda t: print(cli.save_token(t)), token)
        app.ask("Hubra API token (hbr_…), kept in ~/.config/hubra/token", got, password=True)

    @staticmethod
    def with_dry_run(app, fn):
        """hubra-cli keeps its own DRY_RUN; follow the TUI's."""
        def run(*args):
            cli.DRY_RUN = bool(getattr(app, "dry_run", False))
            return fn(*args)
        return run

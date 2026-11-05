"""hubra: one command for the hubra-app GitHub repos and the running Hubra.

Needs Python 3.9+ and the gh CLI, logged in as an owner of hubra-app
(`gh auth login`; if asked: `gh auth refresh -s admin:org,workflow`).
Every call goes through gh, so no token is handled here.

    hubra status                    # overview of every repo
    hubra prs                       # every open PR, with links
    hubra pr budgets#12             # one PR in depth (--diff, --web)
    hubra merge                     # auto-merge ready plugin PRs
    hubra merge budgets#12 hubra#190  # only these PRs (--now, --ready)
    hubra release                   # release new plugin.yaml versions
    hubra ship                      # merge, then release
    hubra approve                   # approve waiting signing runs
    hubra runs budgets --failed     # recent Actions runs, with links
    hubra open budgets#12           # open a repo/PR/Actions page
    hubra bump budgets minor        # PR: plugin.yaml + CHANGELOG for the next version
    hubra lists                     # are the plugin lists current? (--fix opens PRs)
    hubra doctor                    # settings that drifted from what setup sets
    hubra setup                     # create new plugin repos, configure all
    hubra setup --key signing.key   # ... and store the signing key
    hubra key signing.key           # only store the signing key
    hubra login                     # save a Hubra API token
    hubra hubra health              # also alerts, errors, jobs, whoami, get PATH
    hubra tui                       # everything on one screen (the [tui] extra)

Every command takes repo names after it: a plugin by its short name
(`budgets`), or a core repo (`hubra`, `hubra.club`, `hubra-plugins`).
`name#12` (or a PR URL) names one pull request. Without names, `status`,
`prs` and `runs` cover everything; setup, key, merge, release, ship,
approve and doctor cover all hubra-plugin-* repos. Core repos are merged only when you
name them (`merge hubra` or `merge hubra#190`). `--dry-run` shows what would
change.

status   Per repo: version on main, latest release, unreleased version, open
         PRs (ready / draft) and the check state of the newest main commit.
         One GraphQL query for the whole org, so it takes a second or two.
prs      Every open PR: checks, review, conflicts, auto-merge, link.
pr       One PR in depth: branches, merge state and what blocks it, every
         check with its link, reviews, commits, changed files and the
         description. --diff prints the diff, --web opens it in the browser.
merge    Turns on auto-merge (squash) for every open PR that isn't a draft:
         GitHub merges it once the required checks are green. PRs with
         conflicts or failing checks stay open. With name#12 only that PR;
         --ready first marks a named draft ready, --now merges right away
         instead of waiting (GitHub refuses while required checks are red).
         Prints each PR's link and its checks page.
release  Reads plugin.yaml on the default branch. If v<version> has no
         release and is newer than the latest, pushes the tag v<version>;
         release.yml then tests, signs (waits for your approval) and creates
         the release with the zip attached, so immutable releases work.
         Repos still on the older "release published" workflow get the
         release created instead. Prints the release page and the run.
         Repos still on the template's example plugin are skipped.
ship     merge, then release. Auto-merge is asynchronous, so a version bump
         that is still in a PR is released on the next run.
approve  Lists release runs waiting for the plugin-signing environment and
         approves each after you say y (--yes approves without asking).
runs     The latest Actions runs per repo (--limit 3) with links; --failed
         shows only failed ones.
open     Opens a repo, name#12, or with --actions / --releases those pages.
bump     Opens a PR on release/v<new> that sets plugin.yaml to the next
         version (minor by default; major, patch or 1.4.0) and adds a
         CHANGELOG.md section listing the PRs merged since the last release.
         --auto-merge merges it once checks pass; then `release` publishes.
lists    Compares the latest release of each plugin with the hubra README,
         the Marketplace README, the org profile and the hubra.club Modules
         cards. --fix opens PRs for outdated versions in the two READMEs
         (new rows, cards and the org profile stay yours).
doctor   Checks each repo against what setup sets (settings, security,
         rulesets, plugin-signing environment and its key, release.yml as in
         the template) and names the command that fixes it.
setup    Creates the plugins in NEW_PLUGINS that don't exist yet from
         hubra-plugins-template, then configures every named plugin repo:
         no wiki/projects, delete merged branches, auto-merge allowed,
         secret scanning + push protection, private vulnerability reporting,
         rulesets main + tags from the template (replaced by name), and the
         plugin-signing environment (you as reviewer, only v* tags).
key      Stores the private key of Hubra.pub as HUBRA_SIGNING_KEY in each
         repo's plugin-signing environment, piped to `gh secret set` (gh
         encrypts it before it leaves your machine). Refuses the Marketplace
         index key and any key that doesn't match Hubra.pub.

After a release, the plugin lists are still yours (hubra
docs/plugin-releases.md: hubra README, hubra-plugins README, hubra.club
Modules page).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from hubra_cli import __version__

ORG = "hubra-app"
PREFIX = "hubra-plugin-"
TEMPLATE = f"{ORG}/hubra-plugins-template"
CORE = ("hubra", "hubra.club", "hubra-plugins")
WEB = "https://github.com"
WORKERS = 8  # repos handled at the same time by status-like commands

# Plugins `setup` creates when they don't exist yet.
NEW_PLUGINS = {
    "documents": "Hubra plugin: document library with folders and access per role or group",
    "payments": "Hubra plugin: TWINT and card payments for invoices and donations (Stripe Connect)",
    "board": "Hubra plugin: board tools - general meeting, to-dos, year planner, contracts",
    "kasse": "Hubra plugin: expense claims, drinks tab and collective orders",
    "supporters": "Hubra plugin: sponsors and fundraising goals",
    "booking": "Hubra plugin: room and equipment booking",
    "teams": "Hubra plugin: match days - fixtures, availability, line-ups, carpooling",
    "community": "Hubra plugin: galleries, honours, skills directory, idea box, welcome checklist",
    "forms": "Hubra plugin: forms and surveys",
    "letters": "Hubra plugin: letters by post through a Swiss print-and-post service",
    "messengers": "Hubra plugin: announcements to Telegram, Signal and Matrix",
    "assistant": "Hubra plugin: board assistant for drafts and summaries (opt-in)",
}

# Public halves of the official keys (hubra/core/plugins/keys/official/).
PLUGIN_PUB = "MCowBQYDK2VwAyEASBCzJyc3fMF3PSsZyyQyKag42FWmR2fvk+vVnwh0VJU="  # Hubra.pub
INDEX_PUB = "MCowBQYDK2VwAyEARHTn/5NrXnoZAOM3M28+lxCKH/Un8sFN2R0otaoLwEI="  # Hubra index.pub

DRY_RUN = False


# ── output ───────────────────────────────────────────────────────────────────
# Repos run side by side; each one's lines are collected and printed together,
# in the order the repos were named.

_out = threading.local()


def say(msg: str = "") -> None:
    lines = getattr(_out, "lines", None)
    if lines is None:
        print(msg, flush=True)
    else:
        lines.append(msg)


def for_each(repos: list[str], action, ctx: dict, parallel: bool = True) -> None:
    def one(repo: str) -> list[str]:
        _out.lines = [f"== {repo}"]
        try:
            action(repo, ctx)
        except RuntimeError as err:
            say(f"   error: {err}")
        lines, _out.lines = _out.lines, None
        return lines

    if not parallel:
        for repo in repos:
            say(f"== {repo}")
            try:
                action(repo, ctx)
            except RuntimeError as err:
                say(f"   error: {err}")
        return
    with ThreadPoolExecutor(WORKERS) as pool:
        for lines in pool.map(one, repos):
            say("\n".join(lines))


# ── gh ───────────────────────────────────────────────────────────────────────


def gh(*args: str, stdin: str | None = None, check: bool = True) -> str:
    """Run gh and return its output; raises with gh's message on failure."""
    proc = subprocess.run(["gh", *args], input=stdin, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args[:4])}: {proc.stderr.strip()[:300]}")
    return proc.stdout


def api(method: str, path: str, body: dict | None = None):
    """One GitHub API call through `gh api`; returns the JSON answer."""
    if DRY_RUN and method != "GET":
        say(f"   [dry-run] {method} {path}")
        return {}
    args = ["api", "-X", method, path, "-H", "Accept: application/vnd.github+json"]
    if body is not None:
        args += ["--input", "-"]
    out = gh(*args, stdin=json.dumps(body) if body is not None else None)
    return json.loads(out) if out.strip() else None


def graphql(query: str, **variables) -> dict:
    """A read-only GraphQL query (runs in dry runs too)."""
    out = gh("api", "graphql", "--input", "-",
             stdin=json.dumps({"query": query, "variables": variables}))
    data = json.loads(out)
    if data.get("errors"):
        raise RuntimeError("; ".join(e.get("message", "?") for e in data["errors"])[:300])
    return data["data"]


REPOS_LIGHT = """
query($org: String!, $cursor: String) {
  organization(login: $org) {
    repositories(first: 100, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes { name isArchived }
    }
  }
}"""

REPOS_FULL = """
query($org: String!, $cursor: String) {
  organization(login: $org) {
    repositories(first: 25, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        name isArchived
        defaultBranchRef { name target { ... on Commit { statusCheckRollup { state } } } }
        manifest: object(expression: "HEAD:plugin.yaml") { ... on Blob { text } }
        releases(first: 30, orderBy: {field: CREATED_AT, direction: DESC}) { nodes { tagName } }
        pullRequests(states: OPEN, first: 30, orderBy: {field: UPDATED_AT, direction: DESC}) {
          totalCount
          nodes {
            number title url isDraft mergeable reviewDecision headRefName
            author { login }
            autoMergeRequest { enabledAt }
            commits(last: 1) { nodes { commit { statusCheckRollup { state } } } }
          }
        }
      }
    }
  }
}"""

_repos_cache: dict[bool, dict] = {}


def org_repos(full_info: bool = False) -> dict[str, dict]:
    """Every non-archived repo of the org by name, in one paged query."""
    if full_info not in _repos_cache:
        query, found, cursor = (REPOS_FULL if full_info else REPOS_LIGHT), {}, None
        while True:
            page = graphql(query, org=ORG, cursor=cursor)["organization"]["repositories"]
            for node in page["nodes"]:
                if not node["isArchived"]:
                    found[node["name"]] = node
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
        _repos_cache[full_info] = found
    return _repos_cache[full_info]


def exists(repo: str) -> bool:
    return subprocess.run(["gh", "repo", "view", repo], capture_output=True, check=False).returncode == 0


def file_on(repo: str, path: str, ref: str | None = None) -> str | None:
    q = f"?ref={ref}" if ref else ""
    try:
        return base64.b64decode(api("GET", f"/repos/{repo}/contents/{path}{q}")["content"]).decode()
    except (RuntimeError, KeyError, TypeError):
        return None


def plugin_names() -> list[str]:
    return sorted(n[len(PREFIX):] for n in org_repos() if n.startswith(PREFIX))


def full(name: str) -> str:
    name = name.removeprefix(f"{ORG}/")
    return f"{ORG}/{name}" if name in CORE or name.startswith(PREFIX) else f"{ORG}/{PREFIX}{name}"


def short(repo: str) -> str:
    return repo.split("/", 1)[1]


def parse_target(arg: str) -> tuple[str, int | None]:
    """`budgets`, `budgets#12` or a PR URL -> (owner/repo, PR number or None)."""
    m = re.match(rf"(?:https?://github\.com/)?(?:{ORG}/)?([\w.-]+)/pull/(\d+)", arg)
    if m:
        return full(m.group(1)), int(m.group(2))
    name, _, num = arg.partition("#")
    if num and not num.isdigit():
        sys.exit(f"Not a PR number: {arg}")
    return full(name), int(num) if num else None


def is_plugin(repo: str) -> bool:
    return short(repo).startswith(PREFIX)


def version_tuple(v: str) -> tuple:
    parts = [int(p) for p in re.findall(r"\d+", v.lstrip("v").split("-")[0])[:3]]
    return tuple(parts + [0] * (3 - len(parts)))


def parse_manifest(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None
    pid = re.search(r"^id:\s*[\"']?([\w-]+)", text, re.MULTILINE)
    ver = re.search(r"^version:\s*[\"']?([^\"'\s]+)", text, re.MULTILINE)
    return (pid.group(1), ver.group(1)) if pid and ver else None


def manifest(repo: str, ref: str) -> tuple[str, str] | None:
    return parse_manifest(file_on(repo, "plugin.yaml", ref))


def release_tags(repo: str) -> list[str]:
    return [r["tag_name"] for r in api("GET", f"/repos/{repo}/releases?per_page=100") or []]


def newest(tags: list[str]) -> str | None:
    return max(tags, key=version_tuple, default=None)


def ago(stamp: str | None) -> str:
    if not stamp:
        return "-"
    secs = (datetime.now(timezone.utc) - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


def table(head: list[str], rows: list[list[str]]) -> None:
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    for r in [head, ["-" * w for w in widths], *rows]:
        say("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


# ── key ──────────────────────────────────────────────────────────────────────


def read_key(path: str) -> str:
    try:
        with open(path) as f:
            key = f.read().strip()
    except OSError as err:
        sys.exit(f"Can't read the signing key: {err}")
    if "PRIVATE KEY" not in key:
        sys.exit(f"{path} is not a private key in PEM format")
    try:
        proc = subprocess.run(["openssl", "pkey", "-pubout"], input=key, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        proc = None  # no openssl: the release workflow still checks the key
    if proc is not None and proc.returncode != 0:
        sys.exit(f"{path} is not a valid private key")
    pub = proc.stdout if proc else ""
    if pub and PLUGIN_PUB not in pub:
        if INDEX_PUB in pub:
            sys.exit(f"{path} is the Marketplace index key; plugins need the plugin key (Hubra.pub)")
        sys.exit(f"{path} does not match Hubra.pub (key id b84edc01a8b17490)")
    return key + "\n"


def set_key(repo: str, key: str) -> None:
    if DRY_RUN:
        say("   [dry-run] gh secret set HUBRA_SIGNING_KEY --env plugin-signing")
        return
    gh("secret", "set", "HUBRA_SIGNING_KEY", "--env", "plugin-signing", "--repo", repo, stdin=key)
    say("   signing key stored (HUBRA_SIGNING_KEY in plugin-signing)")


# ── setup ────────────────────────────────────────────────────────────────────


def setup(repo: str, ctx: dict) -> None:
    name = short(repo).removeprefix(PREFIX)
    if not exists(repo):
        if name not in NEW_PLUGINS:
            raise RuntimeError("does not exist and is not in NEW_PLUGINS")
        if DRY_RUN:
            say(f"   [dry-run] gh repo create {repo} --public --template {TEMPLATE}")
        else:
            gh("repo", "create", repo, "--public", "--template", TEMPLATE,
               "--description", NEW_PLUGINS[name])
            time.sleep(5)  # GitHub copies the template in the background
        say("   created from the template")

    api("PATCH", f"/repos/{repo}", {
        "delete_branch_on_merge": True,
        "allow_auto_merge": True,
        "has_wiki": False,
        "has_projects": False,
        "security_and_analysis": {
            "secret_scanning": {"status": "enabled"},
            "secret_scanning_push_protection": {"status": "enabled"},
        },
    })
    api("PUT", f"/repos/{repo}/private-vulnerability-reporting")

    try:
        current = {r["name"]: r["id"] for r in api("GET", f"/repos/{repo}/rulesets") or []}
    except RuntimeError:
        current = {}  # dry run of a repo that doesn't exist yet
    for ruleset in ctx["rulesets"]:
        if ruleset["name"] in current:
            api("PUT", f"/repos/{repo}/rulesets/{current[ruleset['name']]}", ruleset)
        else:
            api("POST", f"/repos/{repo}/rulesets", ruleset)
    say("   settings, security and rulesets main + tags set")

    api("PUT", f"/repos/{repo}/environments/plugin-signing", {
        "reviewers": [{"type": "User", "id": ctx["me"]["id"]}],
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
    })
    try:
        api("POST", f"/repos/{repo}/environments/plugin-signing/deployment-branch-policies",
            {"name": "v*", "type": "tag"})
    except RuntimeError as err:
        if "already exists" not in str(err) and "422" not in str(err):
            raise
    say("   plugin-signing environment set (v* tags, you as reviewer)")
    if ctx.get("key"):
        set_key(repo, ctx["key"])


# ── merge ────────────────────────────────────────────────────────────────────

PR_FIELDS = "number,title,url,state,isDraft,mergeable,autoMergeRequest"


def merge_one(repo: str, p: dict, ctx: dict, named: bool) -> None:
    label = f"#{p['number']} {p['title']}"
    link = f"\n      {p['url']}" if p.get("url") else ""
    if p.get("state", "OPEN") != "OPEN":
        say(f"   {label}: {p['state'].lower()}, nothing to do{link}")
        return
    if p["isDraft"]:
        if not (named and ctx.get("ready")):
            hint = " (add --ready to merge it anyway)" if named else ""
            say(f"   {label}: draft, left open{hint}{link}")
            return
        if DRY_RUN:
            say(f"   [dry-run] mark {label} ready for review")
        else:
            gh("pr", "ready", str(p["number"]), "--repo", repo)
            say(f"   {label}: marked ready for review")
    if p.get("autoMergeRequest") and not ctx.get("now"):
        say(f"   {label}: auto-merge already on{link}\n      checks: {p['url']}/checks")
        return
    if p.get("mergeable") == "CONFLICTING":
        say(f"   {label}: has conflicts, left open{link}")
        return
    how = "merge now" if ctx.get("now") else "auto-merge"
    if DRY_RUN:
        say(f"   [dry-run] {how} {label}{link}")
        return
    cmd = ["gh", "pr", "merge", str(p["number"]), "--repo", repo, "--squash", "--delete-branch"]
    if not ctx.get("now"):
        cmd.append("--auto")
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        say(f"   {label}: not merged: {proc.stderr.strip()[:200]}{link}")
    elif ctx.get("now"):
        say(f"   {label}: merged{link}")
    else:
        say(f"   {label}: auto-merge on (merges when checks are green){link}"
            f"\n      checks: {p['url']}/checks")


def merge(repo: str, ctx: dict) -> None:
    numbers = ctx.get("pick", {}).get(repo)
    if numbers:
        prs = [json.loads(gh("pr", "view", str(n), "--repo", repo, "--json", PR_FIELDS))
               for n in numbers]
    else:
        prs = json.loads(gh("pr", "list", "--repo", repo, "--state", "open", "--json", PR_FIELDS))
    if not prs:
        say("   no open PRs")
        return
    if any(p.get("state", "OPEN") == "OPEN" for p in prs) and not ctx.get("now"):
        api("PATCH", f"/repos/{repo}", {"allow_auto_merge": True})
    for p in prs:
        merge_one(repo, p, ctx, named=bool(numbers))


# ── release ──────────────────────────────────────────────────────────────────


def release_run(repo: str, tag: str, wait: int = 0) -> dict | None:
    """The release workflow run for `tag` (tag push or, before immutable releases, release
    published); waits up to `wait` seconds for it to start."""
    deadline = time.time() + wait
    while True:
        runs = api("GET", f"/repos/{repo}/actions/runs?branch={tag}&per_page=10") or {}
        for run in runs.get("workflow_runs") or []:
            if run.get("head_branch") == tag and run.get("event") in ("push", "release"):
                return run
        if time.time() >= deadline:
            return None
        time.sleep(3)


def run_state(run: dict) -> str:
    if run.get("status") == "waiting":
        return "waiting for your approval (plugin-signing)"
    return run.get("conclusion") or run.get("status") or "?"


def tag_exists(repo: str, tag: str) -> bool:
    try:
        api("GET", f"/repos/{repo}/git/ref/tags/{tag}")
        return True
    except RuntimeError as err:
        if "404" in str(err) or "Not Found" in str(err):
            return False
        raise


def releases_on_tag_push(repo: str, branch: str) -> bool:
    """True when release.yml starts on a v* tag push and creates the release itself (the
    flow for immutable releases); False for the older "release published" flow."""
    found = get_file(repo, ".github/workflows/release.yml", branch)
    if not found:
        return True
    on = found[0].split("\njobs:", 1)[0]
    return "release:" not in on and "tags:" in on


def release(repo: str, ctx: dict) -> None:
    if not is_plugin(repo):
        say("   not a plugin, skipped (core releases follow docs/release-guidelines.md)")
        return
    branch = api("GET", f"/repos/{repo}")["default_branch"]
    m = manifest(repo, branch)
    if not m:
        say("   no plugin.yaml, skipped")
        return
    pid, ver = m
    if pid == "example":
        say("   still the template's example plugin, skipped")
        return
    tag, tags = f"v{ver}", release_tags(repo)
    last = newest(tags)
    page = f"{WEB}/{repo}/releases/tag/{tag}"
    name = short(repo).removeprefix(PREFIX)
    if tag in tags:
        run = release_run(repo, tag)
        say(f"   {tag} already released\n      {page}")
        if run:
            say(f"      release run: {run_state(run)}\n      {run['html_url']}")
        assets = (api("GET", f"/repos/{repo}/releases/tags/{tag}") or {}).get("assets") or []
        if not any(a.get("name", "").endswith(".zip") for a in assets):
            say("      NO SIGNED ZIP: the Marketplace can't install this release. Once releases are"
                f" immutable it can't be fixed in place; release a new version: hubra bump {name} patch")
        return
    if last and version_tuple(ver) <= version_tuple(last):
        say(f"   plugin.yaml says {ver}, not newer than {last}: skipped")
        return
    on_tag = releases_on_tag_push(repo, branch)
    if on_tag and tag_exists(repo, tag):
        # Tag pushed, release not there yet: the workflow is running, waits for you or failed.
        run = release_run(repo, tag)
        say(f"   {tag} is tagged, the workflow creates the release once it is signed")
        if run:
            say(f"      release run: {run_state(run)}\n      {run['html_url']}")
            if run.get("conclusion") == "failure":
                say("      it failed: fix it, then delete the tag and run release again")
        return
    how = "push tag" if on_tag else "publish release"
    if DRY_RUN:
        say(f"   [dry-run] {how} {tag} on {branch} (latest {last or 'none'})\n      would be {page}")
        return
    if on_tag:
        # Immutable releases: the workflow tests, signs and then creates the release with the
        # zip attached, so only the tag is made here (a tag pushed with your token starts it).
        head = api("GET", f"/repos/{repo}/git/ref/heads/{branch}")["object"]["sha"]
        api("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/tags/{tag}", "sha": head})
        say(f"   tagged {tag} on {branch} at {head[:7]} (was {last or 'none'}); the release appears at\n      {page}")
    else:
        gh("release", "create", tag, "--repo", repo, "--target", branch,
           "--title", f"{pid} {ver}", "--notes", f"{pid} {ver}")
        say(f"   released {tag} (was {last or 'none'})\n      {page}")
    run = release_run(repo, tag, wait=20)
    if run:
        say(f"      release run: {run_state(run)}\n      {run['html_url']}")
    else:
        say(f"      release run not started yet: {WEB}/{repo}/actions/workflows/release.yml")
    say(f"      approve the signing step there or with: hubra approve {name}")


def ship(repo: str, ctx: dict) -> None:
    merge(repo, ctx)
    release(repo, ctx)


def key_only(repo: str, ctx: dict) -> None:
    set_key(repo, ctx["key"])


# ── approve ──────────────────────────────────────────────────────────────────


def waiting_runs(repo: str) -> list[tuple[str, dict, list[dict]]]:
    out = []
    runs = api("GET", f"/repos/{repo}/actions/runs?status=waiting&per_page=20") or {}
    for run in runs.get("workflow_runs") or []:
        pending = api("GET", f"/repos/{repo}/actions/runs/{run['id']}/pending_deployments") or []
        envs = [p["environment"] for p in pending if p.get("current_user_can_approve")]
        if envs:
            out.append((repo, run, envs))
    return out


def approve_run(repo: str, run: dict, envs: list[dict]) -> None:
    api("POST", f"/repos/{repo}/actions/runs/{run['id']}/pending_deployments", {
        "environment_ids": [e["id"] for e in envs],
        "state": "approved",
        "comment": "Approved with the hubra CLI",
    })


def all_waiting(repos: list[str]) -> list[tuple[str, dict, list[dict]]]:
    with ThreadPoolExecutor(WORKERS) as pool:
        return [w for ws in pool.map(waiting_runs, repos) for w in ws]


def approve(repos: list[str], yes: bool) -> None:
    found = all_waiting(repos)
    if not found:
        print("No runs are waiting for your approval.")
        return
    for repo, run, envs in found:
        names = ", ".join(e["name"] for e in envs)
        print(f"== {repo}  {run.get('head_branch')}  {run.get('name')} ({ago(run.get('created_at'))})")
        print(f"   waits for: {names}\n   {run['html_url']}")
        if DRY_RUN:
            print("   [dry-run] would approve")
            continue
        if not yes:
            try:
                answer = input(f"   Approve {names} for {short(repo)} {run.get('head_branch')}? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                print("   left waiting")
                continue
        approve_run(repo, run, envs)
        print("   approved")


# ── status / prs / pr / runs / open ──────────────────────────────────────────


def checks_word(rollup: dict | None) -> str:
    state = (rollup or {}).get("state")
    return {"SUCCESS": "passing", "FAILURE": "failing", "ERROR": "failing",
            "PENDING": "running", "EXPECTED": "waiting"}.get(state, "no checks")


def status_rows(repos: list[str]) -> list[list[str]]:
    info = org_repos(full_info=True)
    rows = []
    for repo in repos:
        node = info.get(short(repo))
        if not node:
            rows.append([short(repo), "missing", "", "", "", ""])
            continue
        m = parse_manifest((node.get("manifest") or {}).get("text")) if is_plugin(repo) else None
        ver = m[1] if m and m[0] != "example" else ("template" if m else "-")
        last = newest([r["tagName"] for r in node["releases"]["nodes"]]) or "-"
        todo = ""
        if m and m[0] != "example" and (last == "-" or version_tuple(m[1]) > version_tuple(last)):
            todo = f"v{m[1]}"
        prs = node["pullRequests"]
        drafts = sum(p["isDraft"] for p in prs["nodes"])
        target = (node.get("defaultBranchRef") or {}).get("target") or {}
        ci = ((target.get("statusCheckRollup") or {}).get("state") or "-").lower()
        rows.append([short(repo), ver, last, todo, f"{prs['totalCount'] - drafts}/{drafts}", ci])
    return rows


STATUS_HEAD = ["repo", "main", "released", "to release", "PRs ready/draft", "CI main"]


def status(repos: list[str]) -> None:
    table(STATUS_HEAD, status_rows(repos))


def capture(fn, *args, **kwargs) -> str:
    """Run one of the printing commands and return what it would print."""
    old = getattr(_out, "lines", None)
    _out.lines = []
    try:
        fn(*args, **kwargs)
    except RuntimeError as err:
        say(f"error: {err}")
    finally:
        lines, _out.lines = _out.lines, old
    return "\n".join(lines)


def prs(repos: list[str]) -> None:
    info = org_repos(full_info=True)
    total = 0
    for repo in repos:
        nodes = ((info.get(short(repo)) or {}).get("pullRequests") or {}).get("nodes") or []
        if not nodes:
            continue
        say(f"== {short(repo)}")
        for p in sorted(nodes, key=lambda p: p["number"]):
            total += 1
            commit = (p["commits"]["nodes"] or [{}])[0].get("commit") or {}
            bits = ["draft" if p["isDraft"] else "ready", f"checks {checks_word(commit.get('statusCheckRollup'))}"]
            if p.get("reviewDecision"):
                bits.append(p["reviewDecision"].lower().replace("_", " "))
            if p.get("mergeable") == "CONFLICTING":
                bits.append("CONFLICTS")
            if p.get("autoMergeRequest"):
                bits.append("auto-merge on")
            who = (p.get("author") or {}).get("login", "?")
            say(f"   #{p['number']} {p['title']}  ({who}, {p['headRefName']})")
            say(f"      {' · '.join(bits)}\n      {p['url']}")
    say(f"{total} open PR{'s' if total != 1 else ''}")


MERGE_STATE = {
    "CLEAN": "ready to merge",
    "BLOCKED": "blocked: required checks or reviews missing",
    "BEHIND": "behind the base branch: update it first",
    "DIRTY": "has merge conflicts",
    "UNSTABLE": "mergeable, but some checks fail",
    "HAS_HOOKS": "mergeable (with hooks)",
    "DRAFT": "draft",
    "UNKNOWN": "GitHub is still working it out",
}


def pr_detail(repo: str, number: int, diff: bool, web: bool, full_body: bool) -> None:
    if web:
        gh("pr", "view", str(number), "--repo", repo, "--web")
        return
    p = json.loads(gh("pr", "view", str(number), "--repo", repo, "--json",
                      "number,title,url,state,isDraft,author,baseRefName,headRefName,createdAt,"
                      "updatedAt,mergeable,mergeStateStatus,reviewDecision,autoMergeRequest,"
                      "additions,deletions,changedFiles,files,commits,statusCheckRollup,reviews,"
                      "labels,body"))
    say(f"#{p['number']} {p['title']}\n{p['url']}\n")
    who = (p.get("author") or {}).get("login", "?")
    say(f"  state    {p['state'].lower()}{' (draft)' if p['isDraft'] else ''}, by {who}, "
          f"opened {ago(p.get('createdAt'))}, updated {ago(p.get('updatedAt'))}")
    say(f"  branch   {p['headRefName']} -> {p['baseRefName']}")
    say(f"  merge    {MERGE_STATE.get(p.get('mergeStateStatus'), p.get('mergeStateStatus') or '?')}"
          f"{'; auto-merge on' if p.get('autoMergeRequest') else ''}")
    say(f"  review   {(p.get('reviewDecision') or 'none required').lower().replace('_', ' ')}")
    if p.get("labels"):
        say(f"  labels   {', '.join(lab['name'] for lab in p['labels'])}")
    say(f"  size     {p['changedFiles']} files, +{p['additions']} -{p['deletions']}, "
          f"{len(p.get('commits') or [])} commit{'' if len(p.get('commits') or []) == 1 else 's'}")

    checks = p.get("statusCheckRollup") or []
    say(f"\nChecks ({len(checks)}):  {p['url']}/checks")
    for c in sorted(checks, key=lambda c: (c.get("conclusion") or c.get("state") or "") == "SUCCESS"):
        name = c.get("name") or c.get("context") or "?"
        if c.get("workflowName"):
            name = f"{c['workflowName']} / {name}"
        state = (c.get("conclusion") or c.get("state") or c.get("status") or "?").lower()
        say(f"  {state:<10} {name}")
        link = c.get("detailsUrl") or c.get("targetUrl")
        if link and state not in ("success", "skipped", "neutral"):
            say(f"             {link}")

    reviews = [r for r in p.get("reviews") or [] if r.get("state") != "COMMENTED" or r.get("body")]
    if reviews:
        say("\nReviews:")
        for r in reviews:
            say(f"  {r['state'].lower():<18} {(r.get('author') or {}).get('login', '?')}")

    say("\nCommits:")
    for c in (p.get("commits") or [])[-15:]:
        say(f"  {c['oid'][:7]} {c['messageHeadline']}")

    say("\nFiles:")
    for f in sorted(p.get("files") or [], key=lambda f: -(f["additions"] + f["deletions"]))[:40]:
        say(f"  +{f['additions']:<5} -{f['deletions']:<5} {f['path']}")
    if len(p.get("files") or []) > 40:
        say(f"  ... and {len(p['files']) - 40} more")

    body = (p.get("body") or "").strip()
    if body:
        lines = body.splitlines()
        cut = lines if full_body else lines[:25]
        say("\nDescription:\n  " + "\n  ".join(cut))
        if len(cut) < len(lines):
            say(f"  ... {len(lines) - len(cut)} more lines (--body for all)")
    if diff:
        say("\n" + gh("pr", "diff", str(number), "--repo", repo))


def runs(repos: list[str], limit: int, failed: bool) -> None:
    def fetch(repo: str):
        q = f"per_page={limit}" + ("&status=failure" if failed else "")
        return repo, (api("GET", f"/repos/{repo}/actions/runs?{q}") or {}).get("workflow_runs") or []

    with ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(fetch, repos))
    for repo, items in results:
        if not items:
            continue
        say(f"== {short(repo)}")
        for r in items:
            state = "waiting (approval)" if r.get("status") == "waiting" else (r.get("conclusion") or r.get("status"))
            say(f"   {state:<18} {r.get('name')} · {r.get('event')} · {r.get('head_branch')} · "
                  f"{ago(r.get('created_at'))}\n      {r['html_url']}")


def open_page(repo: str, number: int | None, actions: bool, releases: bool) -> None:
    url = f"{WEB}/{repo}"
    if number:
        url += f"/pull/{number}"
    elif actions:
        url += "/actions"
    elif releases:
        url += "/releases"
    say(url)
    webbrowser.open(url)


# ── bump / lists / doctor ────────────────────────────────────────────────────


def get_file(repo: str, path: str, ref: str | None = None) -> tuple[str, str] | None:
    """(text, blob sha) of a file, or None when it doesn't exist."""
    q = f"?ref={ref}" if ref else ""
    try:
        data = api("GET", f"/repos/{repo}/contents/{path}{q}")
    except RuntimeError as err:
        if "404" in str(err) or "Not Found" in str(err):
            return None
        raise
    return base64.b64decode(data["content"]).decode(), data["sha"]


def open_pr(repo: str, base: str, branch: str, files: dict[str, tuple[str, str | None]],
            message: str, title: str, body: str) -> str:
    """Commit `files` ({path: (new text, old blob sha or None)}) on a new branch and open a PR."""
    head = api("GET", f"/repos/{repo}/git/ref/heads/{base}")["object"]["sha"]
    api("POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head})
    for path, (text, sha) in files.items():
        body_ = {"message": message, "branch": branch,
                 "content": base64.b64encode(text.encode()).decode()}
        if sha:
            body_["sha"] = sha
        api("PUT", f"/repos/{repo}/contents/{path}", body_)
    return gh("pr", "create", "--repo", repo, "--base", base, "--head", branch,
              "--title", title, "--body", body).strip()


def next_version(current: str, level: str) -> str:
    if re.fullmatch(r"\d+\.\d+\.\d+", level):
        return level
    major, minor, patch = version_tuple(current)
    if level == "major":
        return f"{major + 1}.0.0"
    if level == "minor":
        return f"{major}.{minor + 1}.0"
    if level == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise RuntimeError(f"bump takes major, minor, patch or a version like 1.4.0, not {level!r}")


def merged_since_release(repo: str, base: str, tag: str | None) -> list[dict]:
    """PRs merged into `base` after the release `tag` was published (all, without a tag)."""
    since = ""
    if tag:
        since = (api("GET", f"/repos/{repo}/releases/tags/{tag}") or {}).get("published_at") or ""
    pulls = api("GET", f"/repos/{repo}/pulls?state=closed&base={base}&sort=updated&direction=desc&per_page=50") or []
    out = [p for p in pulls if p.get("merged_at") and p["merged_at"] > since
           and not re.match(r"(?i)release \d", p.get("title") or "")]
    return sorted(out, key=lambda p: p["merged_at"])


def with_changelog_section(text: str | None, version: str, lines: list[str]) -> str | None:
    """CHANGELOG.md with a `## version` section on top; None when it already has one."""
    text = text or "# Changelog\n"
    if re.search(rf"^## \[?{re.escape(version)}\]?\s*$", text, re.M):
        return None
    section = f"## {version}\n\n" + "\n".join(lines or ["- "]) + "\n\n"
    m = re.search(r"^## ", text, re.M)
    if m:
        return text[:m.start()] + section + text[m.start():]
    return text.rstrip("\n") + "\n\n" + section


def bump(repo: str, ctx: dict) -> None:
    if not is_plugin(repo):
        say("   not a plugin, skipped")
        return
    base = api("GET", f"/repos/{repo}")["default_branch"]
    found = get_file(repo, "plugin.yaml", base)
    m = parse_manifest(found[0]) if found else None
    if not m:
        say("   no plugin.yaml, skipped")
        return
    if m[0] == "example":
        say("   still the template's example plugin, skipped")
        return
    pid, cur = m
    new = next_version(cur, ctx["level"])
    if version_tuple(new) <= version_tuple(cur):
        raise RuntimeError(f"{new} is not newer than {cur} in plugin.yaml")
    last = newest(release_tags(repo))
    if last and version_tuple(cur) > version_tuple(last):
        say(f"   note: {cur} in plugin.yaml isn't released yet (latest {last}); `hubra release` would publish it")
    prs = merged_since_release(repo, base, last)
    notes = [f"- {p['title']} (#{p['number']})" for p in prs]
    yaml_text = re.sub(r"^(version:\s*[\"']?)[^\"'\s]+", rf"\g<1>{new}", found[0], count=1, flags=re.M)
    cl = get_file(repo, "CHANGELOG.md", base)
    cl_text = with_changelog_section(cl[0] if cl else None, new, notes)
    branch = f"release/v{new}"
    say(f"   {pid} {cur} -> {new}, {len(prs)} PR(s) merged since {last or 'the start'}")
    for line in notes or ["   (no merged PRs found: write the changelog lines in the PR)"]:
        say(f"      {line}")
    if DRY_RUN:
        say(f"   [dry-run] branch {branch}: plugin.yaml version {new}"
            f"{', CHANGELOG.md section ## ' + new if cl_text else ''}; PR 'Release {new}'")
        return
    files = {"plugin.yaml": (yaml_text, found[1])}
    if cl_text:
        files["CHANGELOG.md"] = (cl_text, cl[1] if cl else None)
    body = (f"Bumps `plugin.yaml` to {new} and adds the CHANGELOG section (from the PRs merged since "
            f"{last or 'the start'}). Edit the changelog here before merging.\n\n"
            f"After the merge, `hubra release {short(repo).removeprefix(PREFIX)}` publishes v{new}; "
            "the release workflow signs it once you approve.")
    url = open_pr(repo, base, branch, files, f"Release {new}", f"Release {new}", body)
    say(f"   opened {url}")
    if ctx.get("auto"):
        proc = subprocess.run(["gh", "pr", "merge", url.rsplit("/", 1)[-1], "--repo", repo,
                               "--squash", "--auto", "--delete-branch"],
                              capture_output=True, text=True, check=False)
        say("   auto-merge on" if proc.returncode == 0 else f"   auto-merge not set: {proc.stderr.strip()[:200]}")


# Where the plugins are listed (docs/plugin-releases.md in hubra).
VERSION_LISTS = [
    ("hubra README", f"{ORG}/hubra", "README.md"),
    ("Marketplace README", f"{ORG}/hubra-plugins", "README.md"),
    ("org profile", f"{ORG}/.github", "profile/README.md"),
]
CARD_LISTS = [
    ("hubra.club DE", f"{ORG}/hubra.club", "src/pages/module.html"),
    ("hubra.club EN", f"{ORG}/hubra.club", "src/pages/en/modules.html"),
]


def listed_version(text: str, name: str) -> str | None:
    m = re.search(rf"{ORG}/{PREFIX}{re.escape(name)}/releases/tag/v([\w.\-]+)\)", text)
    return m.group(1) if m else None


def lists(fix: bool) -> None:
    info = org_repos(full_info=True)
    released = {}
    for repo_name, node in sorted(info.items()):
        if not repo_name.startswith(PREFIX):
            continue
        m = parse_manifest((node.get("manifest") or {}).get("text"))
        last = newest([r["tagName"] for r in node["releases"]["nodes"]])
        if last and not (m and m[0] == "example"):
            released[repo_name.removeprefix(PREFIX)] = last.lstrip("v")
    sources = {}
    for label, repo, path in VERSION_LISTS + CARD_LISTS:
        try:
            sources[label] = get_file(repo, path)
        except RuntimeError as err:
            sources[label] = None
            say(f"can't read {repo}/{path}: {err}")
    head = ["plugin", "released"] + [label for label, _, _ in VERSION_LISTS + CARD_LISTS]
    rows, stale = [], {}
    for name, ver in released.items():
        row = [name, ver]
        for label, repo, path in VERSION_LISTS:
            src = sources.get(label)
            if not src:
                row.append("?")
                continue
            shown = listed_version(src[0], name)
            if shown == ver:
                row.append("ok")
            else:
                row.append(f"shows {shown}" if shown else "missing")
                if shown:
                    stale.setdefault((repo, path), []).append((name, shown, ver))
        for label, _, _ in CARD_LISTS:
            src = sources.get(label)
            row.append("?" if not src else ("ok" if f'data-plugin="{name}"' in src[0] else "no card"))
        rows.append(row)
    behind = [r for r in rows if any(c != "ok" for c in r[2:])]
    current = [r[0] for r in rows if r not in behind]
    if behind:
        table(head, behind)
    if current:
        say(f"{chr(10) if behind else ''}Current everywhere: {', '.join(current)}")
    todo = sum(1 for r in behind for c in r[2:] if c != "ok")
    if not todo:
        return
    say(f"\n{todo} place(s) to update (hubra docs/plugin-releases.md). New rows and cards need a description,"
        " so they stay yours; the org profile is edited in the GitHub web UI.")
    if not stale:
        return
    for (repo, path), changes in stale.items():
        if repo.endswith("/.github"):
            continue
        what = ", ".join(f"{n} {a} -> {b}" for n, a, b in changes)
        if not fix:
            say(f"  {short(repo)}/{path}: {what}  (--fix opens a PR)")
            continue
        text, sha = sources[next(lbl for lbl, r, p in VERSION_LISTS if (r, p) == (repo, path))]
        for name, _, ver in changes:
            text = re.sub(rf"\[[\w.\-]+\]\((https://github\.com/{ORG}/{PREFIX}{re.escape(name)}/releases/tag/)v[\w.\-]+\)",
                          rf"[{ver}](\g<1>v{ver})", text)
        if DRY_RUN:
            say(f"  [dry-run] PR on {short(repo)}: {path}: {what}")
            continue
        branch = f"docs/plugin-versions-{datetime.now(timezone.utc):%Y%m%d-%H%M}"
        base = api("GET", f"/repos/{repo}")["default_branch"]
        url = open_pr(repo, base, branch, {path: (text, sha)}, "docs: current plugin versions",
                      "docs: current plugin versions", f"Updates the plugin versions to the latest releases: {what}.")
        say(f"  {short(repo)}: {url}")


def doctor_ctx() -> dict:
    workflow = get_file(TEMPLATE, ".github/workflows/release.yml")
    return {"rulesets": [json.loads(file_on(TEMPLATE, f".github/rulesets/{f}.json")) for f in ("main", "tags")],
            "workflow": workflow[1] if workflow else None}


def doctor_all(repos: list[str]) -> None:
    for_each(repos, doctor, doctor_ctx())


def doctor(repo: str, ctx: dict) -> None:
    problems: list[str] = []
    info = api("GET", f"/repos/{repo}")
    settings = (("delete_branch_on_merge", True), ("allow_auto_merge", True),
                ("has_wiki", False), ("has_projects", False))
    for key, want in settings if is_plugin(repo) else ():  # core repos: security only
        if info.get(key) != want:
            problems.append(f"{key} is {info.get(key)}, setup sets {want}")
    sec = info.get("security_and_analysis") or {}
    for key in ("secret_scanning", "secret_scanning_push_protection"):
        if (sec.get(key) or {}).get("status") != "enabled":
            problems.append(f"{key} is off")
    try:
        if not (api("GET", f"/repos/{repo}/private-vulnerability-reporting") or {}).get("enabled"):
            problems.append("private vulnerability reporting is off")
    except RuntimeError as err:
        problems.append(f"private vulnerability reporting: {err}")

    current = {r["name"]: r for r in api("GET", f"/repos/{repo}/rulesets") or []}
    for want in ctx["rulesets"] if is_plugin(repo) else []:  # core repos keep their own rulesets
        have = current.get(want["name"])
        if not have:
            problems.append(f"ruleset {want['name']} is missing")
            continue
        have = api("GET", f"/repos/{repo}/rulesets/{have['id']}") or {}
        if have.get("enforcement") != want.get("enforcement"):
            problems.append(f"ruleset {want['name']} is {have.get('enforcement')}")
        missing = {r["type"] for r in want["rules"]} - {r["type"] for r in have.get("rules") or []}
        if missing:
            problems.append(f"ruleset {want['name']} lacks {', '.join(sorted(missing))}")

    if is_plugin(repo):
        try:
            env = api("GET", f"/repos/{repo}/environments/plugin-signing")
        except RuntimeError:
            env = None
        if not env:
            problems.append("no plugin-signing environment")
        else:
            rules = env.get("protection_rules") or []
            if not any(r.get("type") == "required_reviewers" and r.get("reviewers") for r in rules):
                problems.append("plugin-signing has no required reviewer")
            policies = api("GET", f"/repos/{repo}/environments/plugin-signing/deployment-branch-policies") or {}
            if not any(p.get("name") == "v*" and p.get("type") == "tag" for p in policies.get("branch_policies") or []):
                problems.append("plugin-signing doesn't limit deployments to v* tags")
            secrets = api("GET", f"/repos/{repo}/environments/plugin-signing/secrets") or {}
            if "HUBRA_SIGNING_KEY" not in {s.get("name") for s in secrets.get("secrets") or []}:
                problems.append("HUBRA_SIGNING_KEY is not stored (hubra key)")
        ours = get_file(repo, ".github/workflows/release.yml")
        if ours and ctx.get("workflow") and ours[1] != ctx["workflow"]:
            problems.append("release.yml differs from the template's")
        elif not ours:
            problems.append("no .github/workflows/release.yml")

    if not problems:
        say("   ok")
        return
    for p in problems:
        say(f"   - {p}")
    if any("HUBRA_SIGNING_KEY" in p for p in problems) and len(problems) == 1:
        say(f"   fix: hubra key <keyfile> {short(repo).removeprefix(PREFIX)}")
    elif is_plugin(repo):
        say(f"   fix: hubra setup {short(repo).removeprefix(PREFIX)}"
            + ("   (release.yml: copy it from the template)" if any("release.yml" in p for p in problems) else ""))


# ── Hubra API ────────────────────────────────────────────────────────────────
# Reads the running Hubra with an API token (manage › API-Tokens for a
# platform token, club app Settings › API for a club or personal one). The
# token comes from HUBRA_TOKEN or from `login`, which keeps it in
# ~/.config/hubra/token (only you can read it). Every call here only reads.

HUBRA_API = os.environ.get("HUBRA_API_URL", "https://api.hubra.club").rstrip("/")
MANAGE = os.environ.get("HUBRA_MANAGE_URL", "https://manage.hubra.club").rstrip("/")
TOKEN_FILE = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                          "hubra", "token")
TOKEN_RE = re.compile(r"^hbr_(pat|club|plat|oam|oap)_[A-Za-z0-9]{12}_[A-Za-z0-9]{43}$")
TOKEN_KIND = {"pat": "member", "club": "club", "plat": "platform", "oam": "member", "oap": "platform"}


def hubra_token() -> str | None:
    token = os.environ.get("HUBRA_TOKEN", "").strip()
    if not token:
        try:
            with open(TOKEN_FILE) as f:
                token = f.read().strip()
        except OSError:
            return None
    return token or None


def token_kind(token: str | None) -> str | None:
    m = TOKEN_RE.match(token or "")
    return TOKEN_KIND[m.group(1)] if m else None


def hubra_get(path: str, token: str | None = None, **params):
    """GET a Hubra API path with the token; RuntimeError with Hubra's message on failure."""
    token = token or hubra_token()
    if not token:
        raise RuntimeError("No Hubra token: run `hubra login` or set HUBRA_TOKEN")
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(f"{HUBRA_API}{path}{'?' + query if query else ''}", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": f"hubra-cli/{__version__}",
        "X-Hubra-Client": "cli",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        try:
            body = json.loads(err.read() or b"{}")
            msg = body.get("message") or body.get("error") or body.get("code") or ""
        except ValueError:
            msg = ""
        raise RuntimeError(f"Hubra answered {err.code} for {path}: {msg}".rstrip(": ")) from None
    except (urllib.error.URLError, TimeoutError) as err:
        raise RuntimeError(f"Can't reach {HUBRA_API}: {getattr(err, 'reason', err)}") from None


def self_path(token: str | None) -> str:
    side = "platform" if token_kind(token) == "platform" else "app"
    return f"/api/v1/{side}/api-tokens/self"


def login() -> None:
    import getpass
    token = getpass.getpass("Hubra API token (hbr_…): ").strip()
    if not token_kind(token):
        sys.exit("That doesn't look like a Hubra API token (hbr_plat_…, hbr_club_… or hbr_pat_…)")
    me = hubra_get(self_path(token), token)
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    print(f"Saved to {TOKEN_FILE}: {me.get('kind')} token \"{me.get('name')}\", "
          f"scopes {', '.join(me.get('scopes') or []) or '-'}, expires {(me.get('expiresAt') or 'never')[:10]}")


def logout() -> None:
    try:
        os.remove(TOKEN_FILE)
        print(f"Removed {TOKEN_FILE}")
    except FileNotFoundError:
        print("No saved token")


def since(stamp) -> str:
    """ago() for ISO strings and unix seconds."""
    if isinstance(stamp, (int, float)):
        stamp = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
    return ago(stamp)


def behind_main(commit: str | None) -> str:
    """How far the running build is behind hubra main, via gh; '' when unknown."""
    if not commit:
        return ""
    try:
        cmp = api("GET", f"/repos/{ORG}/hubra/compare/{commit}...main")
    except (RuntimeError, ValueError):
        return ""
    n = (cmp or {}).get("ahead_by")
    if n is None:
        return ""
    return "same as main" if n == 0 else f"{n} commit{'s' if n != 1 else ''} behind main"


def show_whoami() -> None:
    token = hubra_token()
    me = hubra_get(self_path(token), token)
    say(f"{me.get('kind')} token \"{me.get('name')}\" ({me.get('prefix')})")
    if me.get("club"):
        say(f"  club     {me['club'].get('name') if isinstance(me['club'], dict) else me['club']}")
    say(f"  scopes   {', '.join(me.get('scopes') or []) or '-'}")
    say(f"  expires  {(me.get('expiresAt') or 'never')[:10]}, used {me.get('useCount', 0)}x, "
        f"last {since(me.get('lastUsedAt'))}")
    say(f"  api      {HUBRA_API}")


def show_health() -> None:
    h = hubra_get("/api/v1/platform/health")
    commit = h.get("commit") or ""
    lag = behind_main(commit)
    say(f"Hubra {h.get('version')} ({commit[:7]}){' · ' + lag if lag else ''}: {h.get('status')}"
        f"{' (stale)' if h.get('stale') else ''}, checked {since(h.get('checked_at'))}")
    say(f"  {WEB}/{ORG}/hubra/commit/{commit}" if commit else "")
    say("\nServices:")
    for name, svc in sorted((h.get("services") or {}).items()):
        state = "ok  " if svc.get("ok") else "FAIL"
        extra = f"  {svc['error']}" if svc.get("error") else ""
        say(f"  {state} {name:<10} {svc.get('latency_ms', '?')} ms{extra}")
    procs = h.get("processes") or []
    if procs:
        say("\nProcesses:")
        for p in sorted(procs, key=lambda p: p.get("component", "")):
            other = "" if p.get("commit") == commit else f"  OTHER BUILD {(p.get('commit') or '?')[:7]}"
            say(f"  {p.get('component', '?'):<16} {p.get('host', '?'):<28} seen {since(p.get('seen'))}{other}")
    up = h.get("last_upgrade")
    if up:
        say(f"\nLast upgrade: {up.get('message')} ({since(up.get('time'))})")
    say(f"\n  {MANAGE}/app/platform/diagnostics")


def show_alerts() -> None:
    a = hubra_get("/api/v1/platform/alerts/overview")
    counts = a.get("counts") or {}
    def n(count: int, word: str) -> str:
        return f"{count} {word}{'' if count == 1 else 's'}"
    say("Open: " + ", ".join(f"{k} {n(v.get('error', 0), 'error')} / {n(v.get('warning', 0), 'warning')}"
                             for k, v in counts.items()))
    m = a.get("maintenance") or {}
    if m.get("active"):
        say(f"Maintenance ON until {m.get('until') or '?'}: {m.get('message')}")
    for p in a.get("problems") or []:
        say(f"  PROBLEM {p.get('message') or p}")
    say("\nRecent:")
    for e in (a.get("recent") or [])[:15]:
        say(f"  {e.get('level', '?'):<7} {since(e.get('time')):<9} {e.get('message')}")
        for line in ((e.get("details") or {}).get("lines") or [])[:2]:
            if line:
                say(f"            {line}")
    say(f"\n  {MANAGE}/app/platform/alerts")


def show_errors(status: str = "open") -> None:
    data = hubra_get("/api/v1/platform/errors", status=status, sort="last_seen")
    c = data.get("counts") or {}
    say(f"Errors: {c.get('open', 0)} open, {c.get('resolved', 0)} resolved · running build "
        f"{data.get('current_build', '?')}")
    for e in data.get("errors") or []:
        new = " NEW in this build" if e.get("new_in_current") else ""
        say(f"\n  #{e.get('id')} {e.get('count')}x {e.get('exc_type')}{new}")
        say(f"     {e.get('route')} · {e.get('location')}")
        say(f"     last {since(e.get('last_seen'))} in {e.get('last_build')}, first {since(e.get('first_seen'))}")
        say(f"     {(e.get('message') or '').splitlines()[0][:150] if e.get('message') else ''}")
    say(f"\n  {MANAGE}/app/platform/diagnostics/errors")


def show_jobs(hours: int = 24) -> None:
    j = hubra_get("/api/v1/platform/jobs", hours=hours)
    t = j.get("totals") or {}
    say(f"Last {j.get('hours')}h: {t.get('done', 0)} done, {t.get('failed', 0)} failed, "
        f"{t.get('retried', 0)} retried · queued {j.get('queued', 0)}, reserved {j.get('reserved', 0)}")
    running = j.get("running") or []
    if running:
        say("\nRunning:")
        for r in running:
            secs = r.get("seconds") or 0
            flag = "  STUCK?" if secs > 1800 else ""
            say(f"  {secs // 60:>4} min  {r.get('task')}{flag}")
    failed = j.get("failed") or []
    if failed:
        say("\nFailed:")
        for f in failed[:15]:
            error = str(f.get("error") or f.get("exception") or "")[:120]
            say(f"  {since(f.get('time') or f.get('failed_at'))}  {f.get('task')}  {error}")
    tasks = sorted(j.get("tasks") or [], key=lambda x: (-(x.get("failed") or 0), -(x.get("avg_ms") or 0)))
    if tasks:
        say("\nTasks (failures first, then slowest):")
        for x in tasks[:15]:
            say(f"  {x.get('done', 0):>5} done {x.get('failed', 0):>3} failed {x.get('avg_ms', 0):>6} ms  "
                f"{x.get('task', '').removeprefix('hubra.core.tasks.')}")


    say(f"\n  {MANAGE}/app/platform/diagnostics/jobs")


def show_raw(path: str) -> None:
    say(json.dumps(hubra_get(path), indent=2, ensure_ascii=False))


HUBRA_VIEWS = {
    "health": show_health,
    "alerts": show_alerts,
    "errors": show_errors,
    "jobs": show_jobs,
    "whoami": show_whoami,
}


# ── main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    global DRY_RUN
    p = argparse.ArgumentParser(prog="hubra", description="GitHub and Hubra tools for the hubra-app repos",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__.split("\n\n", 1)[1])
    sub = p.add_subparsers(dest="cmd", required=True)
    names_help = "plugin short names, hubra, hubra.club, hubra-plugins; name#12 for one PR"
    for cmd in ("status", "prs", "pr", "merge", "release", "ship", "approve", "runs", "open", "doctor", "setup", "key"):
        sp = sub.add_parser(cmd)
        if cmd == "key":
            sp.add_argument("keyfile", help="private key (PEM) of Hubra.pub")
        if cmd == "pr":
            sp.add_argument("target", help="name#12 or a PR URL")
            sp.add_argument("--diff", action="store_true", help="also print the diff")
            sp.add_argument("--web", action="store_true", help="open it in the browser")
            sp.add_argument("--body", action="store_true", help="the whole description")
            continue
        if cmd == "open":
            sp.add_argument("target", help="name or name#12")
            sp.add_argument("--actions", action="store_true")
            sp.add_argument("--releases", action="store_true")
            continue
        sp.add_argument("repos", nargs="*", help=names_help)
        sp.add_argument("--dry-run", action="store_true")
        if cmd == "setup":
            sp.add_argument("--key", help="also store this signing key")
        if cmd in ("merge", "ship"):
            sp.add_argument("--now", action="store_true", help="merge right away, not via auto-merge")
            sp.add_argument("--ready", action="store_true", help="mark named draft PRs ready first")
        if cmd == "approve":
            sp.add_argument("--yes", action="store_true", help="approve without asking")
        if cmd == "runs":
            sp.add_argument("--limit", type=int, default=3)
            sp.add_argument("--failed", action="store_true")
    sp = sub.add_parser("bump", help="open a PR that bumps a plugin's version and changelog")
    sp.add_argument("name", help="plugin short name, e.g. budgets")
    sp.add_argument("level", nargs="?", default="minor", help="major, minor (default), patch or 1.4.0")
    sp.add_argument("--auto-merge", action="store_true", help="turn on auto-merge for the PR")
    sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("lists", help="check the plugin lists against the releases")
    sp.add_argument("--fix", action="store_true", help="open PRs for outdated versions")
    sp.add_argument("--dry-run", action="store_true")
    sp = sub.add_parser("hubra", help="read the running Hubra with your API token")
    sp.add_argument("view", choices=[*HUBRA_VIEWS, "get"], nargs="?", default="health")
    sp.add_argument("path", nargs="?", help="for get: an API path like /api/v1/platform/jobs")
    sp.add_argument("--hours", type=int, default=24, help="jobs: look back this many hours")
    sp.add_argument("--all", action="store_true", help="errors: also resolved and ignored")
    sub.add_parser("login", help="save a Hubra API token")
    sub.add_parser("logout", help="forget the saved Hubra API token")
    sp = sub.add_parser("tui", help="full-screen view of everything (needs: pip install textual)")
    sp.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    DRY_RUN = getattr(args, "dry_run", False)

    try:
        if args.cmd == "login":
            return login()
        if args.cmd == "logout":
            return logout()
        if args.cmd == "hubra":
            if args.view == "get":
                if not args.path:
                    sys.exit("hubra get /api/v1/platform/...")
                return show_raw(args.path)
            if args.view == "jobs":
                return show_jobs(args.hours)
            if args.view == "errors":
                return show_errors("" if args.all else "open")
            return HUBRA_VIEWS[args.view]()
    except RuntimeError as err:
        sys.exit(str(err))

    if subprocess.run(["gh", "auth", "status"], capture_output=True, check=False).returncode != 0:
        sys.exit("Log in first: gh auth login")

    if args.cmd == "tui":
        try:
            from hubra_cli import tui
        except ImportError as err:
            sys.exit(f"The TUI needs Textual: pipx inject hubra-cli textual (or pip install textual). {err}")
        return tui.run(dry_run=DRY_RUN)

    if args.cmd == "lists":
        try:
            return lists(args.fix)
        except RuntimeError as err:
            sys.exit(str(err))
    if args.cmd == "bump":
        repo = full(args.name)
        if repo not in {full(n) for n in plugin_names()}:
            sys.exit(f"Unknown plugin: {args.name}")
        return for_each([repo], bump, {"level": args.level, "auto": args.auto_merge}, parallel=False)

    if args.cmd in ("pr", "open"):
        repo, number = parse_target(args.target)
        if args.cmd == "open":
            open_page(repo, number, args.actions, args.releases)
        elif not number:
            sys.exit("Name the PR: pr budgets#12")
        else:
            pr_detail(repo, number, args.diff, args.web, args.body)
        return

    plugins = plugin_names()
    if args.cmd == "setup":
        plugins = sorted(set(plugins) | set(NEW_PLUGINS))
    known = {full(n) for n in plugins} | {full(c) for c in CORE}
    pick: dict[str, list[int]] = {}
    if args.repos:
        repos = []
        for arg in args.repos:
            repo, number = parse_target(arg)
            if repo not in repos:
                repos.append(repo)
            if number:
                pick.setdefault(repo, []).append(number)
        unknown = [r for r in repos if r not in known]
        if unknown:
            sys.exit(f"Unknown: {', '.join(unknown)}")
        if pick and args.cmd not in ("merge", "ship"):
            sys.exit("name#12 works with merge, ship, pr and open")
    elif args.cmd in ("status", "prs", "runs"):
        repos = [full(c) for c in CORE] + [full(n) for n in plugins]
    else:
        repos = [full(n) for n in plugins]

    if args.cmd == "status":
        return status(repos)
    if args.cmd == "prs":
        return prs(repos)
    if args.cmd == "runs":
        return runs(repos, args.limit, args.failed)
    if args.cmd == "approve":
        return approve(repos, args.yes)

    ctx: dict = {"pick": pick, "now": getattr(args, "now", False), "ready": getattr(args, "ready", False)}
    keyfile = getattr(args, "keyfile", None) or getattr(args, "key", None)
    if keyfile:
        ctx["key"] = read_key(keyfile)
    if args.cmd in ("setup", "key") and any(not is_plugin(r) for r in repos):
        sys.exit("setup and key are for plugin repos only")
    if args.cmd == "doctor":
        return doctor_all(repos)
    if args.cmd == "setup":
        ctx["me"] = api("GET", "/user")
        ctx["rulesets"] = [json.loads(file_on(TEMPLATE, f".github/rulesets/{f}.json"))
                           for f in ("main", "tags")]

    action = {"setup": setup, "merge": merge, "release": release,
              "ship": ship, "key": key_only}[args.cmd]
    # setup creates repos and key handles the secret: one repo at a time.
    for_each(repos, action, ctx, parallel=args.cmd not in ("setup", "key"))


if __name__ == "__main__":
    main()

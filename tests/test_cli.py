"""Every command against the stand-ins: tests/fake_gh for gh, tests/fake_hubra.py for Hubra."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def hubra(env, *args) -> str:
    proc = subprocess.run([sys.executable, "-m", "hubra_cli", *args], env=env, capture_output=True,
                          text=True, stdin=subprocess.DEVNULL, timeout=60, check=False)
    return proc.stdout + proc.stderr


def test_status_is_one_query_per_page(env):
    log = Path(env["FAKE_GH_LOG"])
    log.write_text("")
    out = hubra(env, "status")
    assert "hubra-plugin-budgets      1.2.0     v1.1.0    v1.2.0" in out
    assert "hubra-plugin-board        template" in out
    calls = log.read_text().splitlines()
    assert len(calls) <= 4, calls  # auth status + 1 light + 2 full pages


def test_prs_lists_links_and_conflicts(env):
    out = hubra(env, "prs")
    assert "https://github.com/hubra-app/hubra-plugin-budgets/pull/14" in out
    assert "CONFLICTS" in out
    assert "4 open PRs" in out


def test_pr_detail(env):
    out = hubra(env, "pr", "budgets#12", "--diff")
    assert "blocked: required checks or reviews missing" in out
    assert "https://github.com/hubra-app/hubra-plugin-budgets/actions/runs/1/job/2" in out
    assert "+version: 1.2.0" in out


def test_merge_named_prs(env):
    out = hubra(env, "merge", "--dry-run", "budgets#13", "budgets#14", "hubra#150")
    assert "draft, left open (add --ready" in out
    assert "has conflicts, left open" in out
    assert "merged, nothing to do" in out
    out = hubra(env, "merge", "--dry-run", "--ready", "--now", "budgets#13")
    assert "[dry-run] mark #13 WIP: charts ready for review" in out
    assert "[dry-run] merge now #13" in out


def test_merge_all_leaves_core_alone(env):
    out = hubra(env, "merge", "--dry-run")
    assert "auto-merge #12 Bump to 1.2.0" in out
    assert "hubra-app/hubra\n" not in out


def test_release_and_ship(env):
    out = hubra(env, "release", "budgets")
    assert "released v1.2.0 (was v1.1.0)" in out
    assert "releases/tag/v1.2.0" in out
    assert "actions/runs/" in out
    out = hubra(env, "ship", "--dry-run", "board", "discord")
    assert "still the template's example plugin" in out
    assert "v2.5.0 already released" in out


def test_approve_and_runs(env):
    assert "[dry-run] would approve" in hubra(env, "approve", "--dry-run")
    assert "actions/runs/9" in hubra(env, "runs", "codes")


def test_setup_dry_run(env):
    out = hubra(env, "setup", "--dry-run", "board")
    assert "plugin-signing environment set" in out


def test_bad_names(env):
    assert "Unknown: hubra-app/hubra-plugin-nope" in hubra(env, "merge", "nope")
    assert "works with merge, ship, pr and open" in hubra(env, "release", "budgets#12")


def test_hubra_views(env):
    out = hubra(env, "hubra", "health")
    assert "12 commits behind main" in out
    assert "OTHER BUILD e1946be" in out
    assert "FAIL imap" in out
    assert "STUCK?" in hubra(env, "hubra", "jobs")
    assert "1 open" in hubra(env, "hubra", "errors")
    assert "relay 1 error / 0 warnings" in hubra(env, "hubra", "alerts")
    assert "platform.diagnostics" in hubra(env, "hubra", "whoami")


def test_hubra_errors_are_readable(env):
    bad = {**env, "HUBRA_TOKEN": "hbr_plat_AAAAAAAAAAAA_" + "C" * 43}
    assert "Hubra answered 401" in hubra(bad, "hubra", "health")
    none = {**env, "HUBRA_TOKEN": ""}
    assert "No Hubra token" in hubra(none, "hubra")

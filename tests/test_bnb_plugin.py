"""hubra as a bnb plugin: the hooks bnb calls, without bnb installed."""

from __future__ import annotations

import argparse


def node(version="1.2.0", tags=("v1.1.0",), plugin_id="budgets"):
    return {"manifest": {"text": f'id: {plugin_id}\nversion: "{version}"\n'},
            "releases": {"nodes": [{"tagName": t} for t in tags]}}


def test_status_columns_and_detail():
    from hubra_cli.bnb import HubraPlugin

    p = HubraPlugin()
    cols = dict(p.status_columns())
    repo = "hubra-app/hubra-plugin-budgets"
    assert cols["version"](repo, node()) == "1.2.0"
    assert cols["to release"](repo, node()) == "v1.2.0"
    assert cols["to release"](repo, node(tags=("v1.2.0",))) == ""
    assert cols["version"](repo, node(plugin_id="example")) == "template"
    assert cols["version"]("hubra-app/hubra", node()) == ""
    assert "to release   v1.2.0  (R pushes the tag, b bumps the version)" in p.repo_detail(repo, node())
    core = {"pyproject": {"text": '[project]\nversion = "0.3.0"\n'}, "releases": {"nodes": [{"tagName": "v0.2.0"}]}}
    assert cols["version"]("hubra-app/hubra", core) == "0.3.0"
    assert cols["to release"]("hubra-app/hubra", core) == "v0.3.0"
    assert "version      0.3.0  (app/backend/pyproject.toml)" in p.repo_detail("hubra-app/hubra", core)
    assert "manifest:" in p.graphql_fields


def test_commands_run_hubra(env, monkeypatch, capsys):
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("FAKE_GH_DELAY", "0")
    from hubra_cli.bnb import HubraPlugin

    parser = argparse.ArgumentParser()
    handlers = HubraPlugin().add_commands(parser.add_subparsers(dest="cmd"))
    assert {"ship", "verify", "key", "setup", "lists", "hubra"} <= set(handlers)
    assert not {"bump", "release"} & set(handlers)  # bnb's own
    args, extra = parser.parse_known_args(["ship", "--dry-run", "budgets"])
    args._extra = extra
    handlers["ship"](args)
    assert "[dry-run] push tag v1.2.0" in capsys.readouterr().out


def test_doctor_checks_signing(env, monkeypatch):
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("FAKE_GH_DELAY", "0")
    from hubra_cli.bnb import HubraPlugin

    p = HubraPlugin()
    assert p.doctor("hubra-app/hubra", None, {}) == []
    problems = p.doctor("hubra-app/hubra-plugin-discord", None, {})
    assert any("HUBRA_SIGNING_KEY" in x for x in problems), problems
    assert "hubra key <keyfile> discord" in p.doctor_hints("hubra-app/hubra-plugin-discord", None, problems)


def test_tui_parts(env, monkeypatch):
    from hubra_cli import bnb, cli

    monkeypatch.setattr(cli, "HUBRA_API", env["HUBRA_API_URL"])
    monkeypatch.setenv("HUBRA_TOKEN", env["HUBRA_TOKEN"])
    monkeypatch.setenv("PATH", env["PATH"])
    monkeypatch.setenv("FAKE_GH_DELAY", "0")
    p = bnb.HubraPlugin()
    tab = p.tui_tabs()[0]
    assert tab["id"] == "hubra" and ("health", "Health") in tab["rows"]
    assert "12 commits behind main" in tab["text"]("health")
    assert tab["url"]("jobs").endswith("/app/platform/diagnostics/jobs")
    assert [k for k, _, _ in p.tui_keys()] == ["S"]
    assert [t.name for t in p.tui_themes()] == ["hubra", "hubra-light"]

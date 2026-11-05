"""The TUI driven headless against the stand-ins (skipped without Textual)."""

from __future__ import annotations

import asyncio
import os

import pytest

pytest.importorskip("textual")


def test_tui_flow(env, monkeypatch):
    for key in ("PATH", "FAKE_GH_DELAY", "FAKE_GH_LOG", "HUBRA_API_URL", "HUBRA_TOKEN"):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setenv("BROWSER", "true")
    from hubra_cli import cli, tui

    monkeypatch.setattr(cli, "HUBRA_API", os.environ["HUBRA_API_URL"])
    cli.DRY_RUN = True

    async def run():
        app = tui.HubraTui(dry_run=True)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(2)
            table = app.query_one("#repos-table")
            assert table.row_count == 23
            table.move_cursor(row=table.get_row_index("hubra-app/hubra-plugin-budgets"))
            await pilot.press("R")
            await pilot.pause(0.3)
            await pilot.press("y")
            await pilot.pause(1.5)
            app.query_one("#tabs").active = "runs"
            runs = app.query_one("#runs-table")
            for _ in range(40):  # Runs load when their tab opens
                await pilot.pause(0.25)
                if runs.row_count:
                    break
            runs.move_cursor(row=runs.get_row_index("hubra-app/hubra-plugin-discord|555"))
            await pilot.press("a")
            await pilot.pause(0.3)
            await pilot.press("y")
            await pilot.pause(1.5)
            app.query_one("#tabs").active = "checks"
            await pilot.pause(1.5)
            assert "shows 1.0.0" in str(app.query_one("#checks-detail").render())
            app.query_one("#tabs").active = "hubra"
            await pilot.pause(1.5)
            assert "12 commits behind main" in str(app.query_one("#hubra-detail").render())
            log = [str(line.text) for line in app.query_one("#log").lines]
            assert any("[dry-run] push tag v1.2.0" in line for line in log), log
            assert any("[dry-run] approve hubra-plugin-discord v2.5.0" in line for line in log), log
            assert app.query_one("#prs-table").row_count == 4

    asyncio.run(run())


async def settle(pilot, app, seconds: float = 20) -> None:
    """Wait until the Checks detail has loaded."""
    for _ in range(int(seconds * 4)):
        await pilot.pause(0.25)
        if "…" not in str(app.query_one("#checks-detail").render()):
            return


def test_keyboard_links_and_theme(env, monkeypatch):
    for key in ("PATH", "FAKE_GH_DELAY", "FAKE_GH_LOG", "HUBRA_API_URL", "HUBRA_TOKEN"):
        monkeypatch.setenv(key, env[key])
    from hubra_cli import cli, tui

    monkeypatch.setattr(cli, "HUBRA_API", os.environ["HUBRA_API_URL"])
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    monkeypatch.setenv("HUBRA_OPEN", "browser")
    opened: list[str] = []
    monkeypatch.setattr(tui.webbrowser, "open", opened.append)
    cli.DRY_RUN = True

    async def run():
        app = tui.HubraTui(dry_run=True)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(2)
            assert app.theme == "hubra"
            table = app.query_one("#repos-table")
            assert app.focused is table  # arrows work without a click first
            await pilot.press("down", "down")
            assert table.cursor_row == 2
            await pilot.press("right")
            await pilot.pause(0.3)
            assert app.query_one("#tabs").active == "prs"
            await pilot.press("left", "left")
            await pilot.pause(0.3)
            assert app.query_one("#tabs").active == "hubra"  # wraps around
            await pilot.press("2")
            await pilot.pause(0.3)
            assert app.query_one("#tabs").active == "prs"
            assert app.focused is app.query_one("#prs-table")
            await pilot.press("4")  # Security
            await pilot.pause(0.3)
            sec = app.query_one("#security-table")
            assert [r.value for r in sec.rows][:3] == ["hubra-app/hubra-plugin-budgets", "hubra-app/hubra",
                                                       "hubra-app/hubra-plugin-discord"]
            assert str(app.query_one("#tabs").get_tab("security").label) == "Security (3)"
            detail = str(app.query_one("#security-detail").render())
            assert "critical" in detail and "SQL query built from user input" in detail
            await pilot.press("3")  # Runs load when their tab opens
            for _ in range(40):
                await pilot.pause(0.25)
                if "to approve" in app.sub_title:
                    break
            await pilot.press("2")
            assert "2 to approve" in app.sub_title  # discord signing + hubra outside contributor run
            await pilot.press("A")  # auto-merge all ready PRs
            await pilot.pause(0.3)
            assert "Turn on auto-merge for 1 ready PR(s)" in str(app.screen.query_one("Label").render())
            await pilot.press("y")
            for _ in range(40):
                await pilot.pause(0.25)
                log = [str(line.text) for line in app.query_one("#log").lines]
                if any("budgets-12" in line or "#12" in line for line in log):
                    break
            await pilot.press("y")  # the selected PR (hubra#190) is no draft: says so instead of nothing
            await pilot.pause(0.2)
            log = [str(line.text) for line in app.query_one("#log").lines]
            assert any("is already ready for review" in line for line in log), log
            await pilot.press("1", "enter")
            await pilot.pause(0.3)
            assert opened and opened[-1].startswith("https://github.com/hubra-app/")
            # a long URL becomes one short clickable label, never split over two lines
            text = tui.linkify("PR https://github.com/hubra-app/hubra-plugin-budgets/pull/12 done")
            assert text.plain == "PR hubra-plugin-budgets/pull/12 done"
            meta = [span.style.meta for span in text.spans if hasattr(span.style, "meta")]
            assert meta == [{"@click": "app.open_link('https://github.com/hubra-app/hubra-plugin-budgets/pull/12')"}]
            await pilot.press("5", "down")  # Checks › Repo settings
            await settle(pilot, app)
            assert "f applies the fixes" in str(app.query_one("#checks-detail").render())
            await pilot.press("f")
            await pilot.pause(0.3)
            await pilot.press("y")
            for _ in range(80):
                await pilot.pause(0.25)
                log = [str(line.text) for line in app.query_one("#log").lines]
                if any("fixed" in line for line in log):
                    break
            assert any("[dry-run] PATCH /repos/hubra-app/hubra-plugin-budgets" in line for line in log), log
            await pilot.press("down")  # Release zips
            await settle(pilot, app)
            assert "NO SIGNED ZIP" in str(app.query_one("#checks-detail").render())
            monkeypatch.setenv("HUBRA_OPEN", "browser")
            app.action_open_link("https://example.org/x")
            assert opened[-1] == "https://example.org/x"
            # over SSH: no browser on the server, the URL goes to the local clipboard (OSC 52)
            monkeypatch.delenv("HUBRA_OPEN")
            monkeypatch.setenv("SSH_CONNECTION", "10.0.0.2 50000 10.0.0.1 22")
            copied: list[str] = []
            monkeypatch.setattr(app, "copy_to_clipboard", copied.append)
            app.action_open_link("https://example.org/y")
            assert copied == ["https://example.org/y"] and opened[-1] == "https://example.org/x"
            assert "copied https://example.org/y" in str(app.query_one("#log").lines[-1].text)
            await pilot.press("t")
            assert app.theme == "hubra-light"
            await pilot.pause(1)

    asyncio.run(run())


def test_pr_filter_bump_and_palette(env, monkeypatch):
    for key in ("PATH", "FAKE_GH_DELAY", "FAKE_GH_LOG", "HUBRA_API_URL", "HUBRA_TOKEN"):
        monkeypatch.setenv(key, env[key])
    from hubra_cli import cli, tui

    monkeypatch.setattr(cli, "HUBRA_API", os.environ["HUBRA_API_URL"])
    cli.DRY_RUN = True

    def cell_text(table, key, col):
        return str(table.get_row(key)[col])

    async def run():
        app = tui.HubraTui(dry_run=True)
        async with app.run_test(size=(160, 45)) as pilot:
            await pilot.pause(2)
            prs = app.query_one("#prs-table")
            assert "auto-merging" in str(app.query_one("#tabs").get_tab("prs").label)
            assert cell_text(prs, "hubra-app/hubra#190", 3) == "auto-merge · waits for checks"
            await pilot.press("v")  # auto-merge: hubra#190 plus the recently merged
            await pilot.pause(0.3)
            assert app.focused is prs
            keys = [r.value for r in prs.rows]
            assert keys == ["hubra-app/hubra#190", "hubra-app/hubra-plugin-budgets#11"], keys
            assert cell_text(prs, "hubra-app/hubra-plugin-budgets#11", 3).startswith("merged ")
            await pilot.press("v", "v", "v")  # merged, drafts, back to all
            await pilot.pause(0.2)
            assert prs.row_count == 4
            await pilot.press("1")
            table = app.query_one("#repos-table")
            table.move_cursor(row=table.get_row_index("hubra-app/hubra-plugin-budgets"))
            await pilot.press("b")
            await pilot.pause(0.3)
            assert isinstance(app.screen, tui.Pick)
            await pilot.press("down", "enter")  # minor
            for _ in range(40):
                await pilot.pause(0.25)
                log = [str(line.text) for line in app.query_one("#log").lines]
                if any("release/v" in line for line in log):
                    break
            assert any("[dry-run] branch release/v1.3.0" in line for line in log), log
            titles = [c.title for c in app.get_system_commands(app.screen)]
            for want in ("Bump plugin version", "Ship plugin", "Set up plugin repo", "Store signing key",
                         "Hubra login", "Verify release zips", "Auto-merge all ready PRs"):
                assert want in titles

    asyncio.run(run())

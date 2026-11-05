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
            await pilot.pause(0.5)
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

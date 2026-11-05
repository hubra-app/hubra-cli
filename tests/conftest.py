from __future__ import annotations

import os
import sys
import threading
from http.server import HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import fake_hubra  # noqa: E402


@pytest.fixture(scope="session")
def hubra_url():
    server = HTTPServer(("127.0.0.1", 0), fake_hubra.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture(scope="session")
def env(tmp_path_factory, hubra_url):
    bin_dir = tmp_path_factory.mktemp("bin")
    (bin_dir / "gh").symlink_to(HERE / "fake_gh")
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_GH_DELAY": "0",
        "FAKE_GH_LOG": str(bin_dir / "calls.log"),
        "HUBRA_API_URL": hubra_url,
        "HUBRA_TOKEN": fake_hubra.TOKEN,
        "XDG_CONFIG_HOME": str(bin_dir / "config"),
    }

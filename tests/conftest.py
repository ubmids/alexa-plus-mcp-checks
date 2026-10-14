import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from fakeserver import FakeMcp  # noqa: E402

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """The checks read ALEXA_MCP_* from the environment; a developer's own
    settings must not leak into these runs."""
    for k in list(os.environ):
        if k.startswith("ALEXA_MCP_"):
            monkeypatch.delenv(k)


@pytest.fixture
def serve():
    """Start a fake MCP server with the given flaws; stopped after the test."""
    started = []

    def start(*flaws):
        srv = FakeMcp(0, flaws).start()
        started.append(srv)
        return srv

    yield start
    for srv in started:
        srv.stop()

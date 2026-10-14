"""`alexa-mcp-check URL`: run the bundled checks and nothing else.

A thin wrapper over pytest. It runs with its own empty ini file and no cache
directory, so whatever project it is run from cannot change what is checked,
and it
passes the token to the plugin through the environment rather than argv.
"""
from __future__ import annotations

import argparse
import os
import sys

import pytest

from . import plugin
from .plugin import CHECKS_PATH

CHECKS_INI = os.path.join(os.path.dirname(CHECKS_PATH), "checks.ini")


def build_args(ns: argparse.Namespace, extra: list[str]) -> list[str]:
    args = [CHECKS_PATH, "-c", CHECKS_INI, "-p", "no:cacheprovider",
            "--rootdir", os.path.dirname(CHECKS_PATH), "-v", "--tb=short",
            "--alexa-mcp-url", ns.url]
    if ns.strict:
        args.append("--alexa-mcp-strict")
    for call in ns.tool_call or []:
        args += ["--alexa-mcp-tool-call", call]
    if ns.summary:
        args += ["--alexa-mcp-summary", os.path.abspath(ns.summary)]
    if ns.latency_budget_ms is not None:
        args += ["--alexa-mcp-latency-budget-ms", str(ns.latency_budget_ms)]
    if ns.latency_samples is not None:
        args += ["--alexa-mcp-latency-samples", str(ns.latency_samples)]
    return args + extra


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="alexa-mcp-check",
        description="Check an MCP server against the Alexa+ MCP toolkit "
                    "requirements and MCP 2025-11-25 Streamable HTTP. Arguments "
                    "after `--` go to pytest.")
    p.add_argument("url", nargs="?", default=os.environ.get("ALEXA_MCP_URL"),
                   help="MCP endpoint, e.g. https://host/mcp (default: $ALEXA_MCP_URL)")
    p.add_argument("--token", default=None,
                   help="bearer token (default: $ALEXA_MCP_TOKEN, which is preferred)")
    p.add_argument("--strict", action="store_true",
                   help="fail SHOULD-level checks instead of warning")
    p.add_argument("--tool-call", action="append", metavar="NAME=JSON",
                   help="a real tool call, e.g. record='{\"limit\": 3}' (repeatable)")
    p.add_argument("--summary", metavar="PATH", help="append a markdown summary to PATH")
    p.add_argument("--latency-budget-ms", type=float, default=None)
    p.add_argument("--latency-samples", type=int, default=None)
    argv = list(sys.argv[1:] if argv is None else argv)
    extra = []
    if "--" in argv:
        i = argv.index("--")
        argv, extra = argv[:i], argv[i + 1:]
    ns = p.parse_args(argv)
    if not ns.url:
        p.error("give the MCP endpoint URL, or set ALEXA_MCP_URL")
    if ns.token:
        os.environ["ALEXA_MCP_TOKEN"] = ns.token
    args = build_args(ns, extra)
    # With entry points switched off the plugin has to be handed over directly.
    plugins = [plugin] if os.environ.get("PYTEST_DISABLE_PLUGIN_AUTOLOAD") else []
    # Run from the package directory, so the report names checks.py::test_...
    # rather than a path relative to wherever the command was typed.
    here = os.getcwd()
    os.chdir(os.path.dirname(CHECKS_PATH))
    try:
        return int(pytest.main(args, plugins=plugins))
    finally:
        os.chdir(here)


if __name__ == "__main__":
    sys.exit(main())

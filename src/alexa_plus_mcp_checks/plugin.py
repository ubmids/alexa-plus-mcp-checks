"""The pytest plugin: options, fixtures, collection and the results table.

Registered through the `pytest11` entry point, so installing the package is
enough. It stays out of the way of any run that does not ask for it: the
bundled checks are only added to a run when `--alexa-mcp-url` is given on the
command line (or when they are named explicitly, as `alexa-mcp-check` and
`pytest --pyargs alexa_plus_mcp_checks.checks` do).
"""
from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from .client import McpClient, McpError

CHECKS_PATH = str(Path(__file__).with_name("checks.py"))
MARKER = "alexa_mcp"
TRUTHY = {"1", "true", "yes", "on"}


@dataclass
class Settings:
    url: str | None = None
    token: str | None = field(default=None, repr=False)
    strict: bool = False
    tool_calls: list[tuple[str, dict]] = field(default_factory=list)
    budget_ms: float = 500.0
    samples: int = 10
    timeout: float = 10.0
    summary: str | None = None

    def __repr__(self) -> str:  # never the token
        return (f"Settings(url={self.url!r}, token={'set' if self.token else 'none'}, "
                f"strict={self.strict}, tool_calls={[n for n, _ in self.tool_calls]})")


SETTINGS = pytest.StashKey[Settings]()


def parse_tool_call(spec: str) -> tuple[str, dict]:
    """`name={"json": "args"}` into (name, args). Arguments are optional."""
    name, sep, raw = spec.partition("=")
    name = name.strip()
    if not name:
        raise pytest.UsageError(f"--alexa-mcp-tool-call needs NAME=JSON, got {spec!r}")
    if not sep or not raw.strip():
        return name, {}
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as e:
        raise pytest.UsageError(
            f"--alexa-mcp-tool-call {name}: arguments are not valid JSON ({e.msg})")
    if not isinstance(args, dict):
        raise pytest.UsageError(
            f"--alexa-mcp-tool-call {name}: arguments must be a JSON object")
    return name, args


def pytest_addoption(parser):
    g = parser.getgroup("alexa-mcp", "Alexa+ MCP server checks")
    g.addoption("--alexa-mcp-url", default=None, metavar="URL",
                help="MCP endpoint to check, e.g. https://host/mcp. Giving it "
                     "adds the bundled checks to the run (env ALEXA_MCP_URL "
                     "supplies the value for alexa-mcp-check and --pyargs runs).")
    g.addoption("--alexa-mcp-token", default=None, metavar="TOKEN",
                help="Bearer token for the server. Prefer env ALEXA_MCP_TOKEN, "
                     "which keeps it out of shell history and process lists.")
    g.addoption("--alexa-mcp-strict", action="store_true", default=False,
                help="Fail SHOULD-level checks instead of reporting them as "
                     "warnings (xfail). Env ALEXA_MCP_STRICT=1 does the same.")
    g.addoption("--alexa-mcp-tool-call", action="append", default=[],
                metavar="NAME=JSON",
                help="A real tool call to make, e.g. record='{\"limit\": 3}'. "
                     "Used to check where a result names its view, and as the "
                     "base for the bad-arguments check. Repeatable. Env "
                     "ALEXA_MCP_TOOL_CALL, one per line.")
    g.addoption("--alexa-mcp-latency-budget-ms", type=float, default=None,
                metavar="MS", help="Round-trip budget (default 500, the Alexa+ figure).")
    g.addoption("--alexa-mcp-latency-samples", type=int, default=None, metavar="N",
                help="tools/list calls to time for the latency check (default 10).")
    g.addoption("--alexa-mcp-timeout", type=float, default=None, metavar="SECONDS",
                help="Per-request timeout (default 10).")
    g.addoption("--alexa-mcp-summary", default=None, metavar="PATH",
                help="Append a markdown summary of the results to PATH "
                     "(the GitHub Action passes $GITHUB_STEP_SUMMARY).")


def _settings(config) -> Settings:
    opt, env = config.getoption, os.environ.get
    calls = list(opt("alexa_mcp_tool_call") or [])
    calls += [ln for ln in (env("ALEXA_MCP_TOOL_CALL") or "").splitlines() if ln.strip()]

    def num(name, envname, default, cast):
        v = opt(name)
        if v is None and env(envname):
            try:
                v = cast(env(envname))
            except ValueError:
                raise pytest.UsageError(f"{envname} must be a number")
        return default if v is None else v

    return Settings(
        url=opt("alexa_mcp_url") or env("ALEXA_MCP_URL") or None,
        token=opt("alexa_mcp_token") or env("ALEXA_MCP_TOKEN") or None,
        strict=bool(opt("alexa_mcp_strict")
                    or (env("ALEXA_MCP_STRICT") or "").strip().lower() in TRUTHY),
        tool_calls=[parse_tool_call(c) for c in calls],
        budget_ms=num("alexa_mcp_latency_budget_ms", "ALEXA_MCP_LATENCY_BUDGET_MS",
                      500.0, float),
        samples=max(1, num("alexa_mcp_latency_samples", "ALEXA_MCP_LATENCY_SAMPLES",
                           10, int)),
        timeout=num("alexa_mcp_timeout", "ALEXA_MCP_TIMEOUT", 10.0, float),
        summary=opt("alexa_mcp_summary") or None,
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", f"{MARKER}(id, title, level, source): an Alexa+ MCP server check")
    config.stash[SETTINGS] = _settings(config)

    if config.getoption("alexa_mcp_url") is not None:
        # The URL on the command line is the request to run the checks. With no
        # paths given, run only the checks (not the project's own test suite);
        # with paths given, run those and the checks.
        named = any("alexa_plus_mcp_checks" in str(a) for a in config.args)
        if config.args_source == pytest.Config.ArgsSource.ARGS:
            if not named:
                config.args.append(CHECKS_PATH)
        else:
            config.args[:] = [CHECKS_PATH]

    config.pluginmanager.register(Recorder(config), "alexa_mcp_recorder")


# --- fixtures -------------------------------------------------------------

@pytest.fixture(scope="session")
def alexa_mcp_settings(request) -> Settings:
    s = request.config.stash[SETTINGS]
    if not s.url:
        pytest.skip("no MCP server to check: pass --alexa-mcp-url or set ALEXA_MCP_URL")
    return s


@pytest.fixture(scope="session")
def alexa_mcp_anon(alexa_mcp_settings):
    """A client that carries no token."""
    c = McpClient(alexa_mcp_settings.url, None, alexa_mcp_settings.timeout)
    yield c
    c.close()


@pytest.fixture(scope="session")
def alexa_mcp_client(alexa_mcp_settings):
    """An initialized client: token, session id and negotiated version, on one
    kept-alive connection, the way Alexa+ would hold it."""
    s = alexa_mcp_settings
    if not s.token:
        pytest.skip("needs a bearer token: pass --alexa-mcp-token or set ALEXA_MCP_TOKEN")
    c = McpClient(s.url, s.token, s.timeout)
    try:
        resp = c.initialize()
    except McpError as e:
        c.close()
        pytest.fail(f"could not initialize, so this check cannot run: {e}", pytrace=False)
    if c.protocol_version is None:
        c.close()
        pytest.fail("initialize did not succeed, so this check cannot run: "
                    + c.excerpt(resp), pytrace=False)
    yield c
    c.close()


@pytest.fixture(scope="session")
def alexa_mcp_tools(alexa_mcp_client) -> list[dict]:
    """Every tool the server lists, all pages."""
    tools = alexa_mcp_client.list_all("tools/list", "tools")
    return [t for t in tools if isinstance(t, dict)]


# --- results table --------------------------------------------------------

def _short(text: str) -> str:
    return " ".join(str(text).split())


def _reason(report) -> str:
    if hasattr(report, "wasxfail"):
        return str(report.wasxfail)
    lr = report.longrepr
    if lr is None:
        return ""
    if isinstance(lr, tuple) and len(lr) == 3:
        return str(lr[2]).removeprefix("Skipped: ")
    crash = getattr(lr, "reprcrash", None)
    if crash is not None and crash.message:
        return crash.message.removeprefix("Failed: ")
    lines = [ln for ln in str(lr).splitlines() if ln.strip()]
    return lines[-1].removeprefix("E   ").removeprefix("Failed: ") if lines else ""


class Recorder:
    """Collects one outcome per check and prints the table at the end."""

    def __init__(self, config):
        self.config = config
        self.meta: dict[str, dict] = {}
        self.results: dict[str, tuple[str, str]] = {}

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, items):
        for item in items:
            m = item.get_closest_marker(MARKER)
            if m is not None:
                self.meta[item.nodeid] = dict(m.kwargs)

    def pytest_runtest_logreport(self, report):
        if report.nodeid not in self.meta:
            return
        current = self.results.get(report.nodeid)
        if report.when == "call" or (report.when == "setup" and not report.passed):
            if hasattr(report, "wasxfail"):
                outcome = "WARN"
            elif report.passed:
                outcome = "PASS"
            elif report.skipped:
                outcome = "SKIP"
            else:
                outcome = "FAIL" if report.when == "call" else "ERROR"
            detail = _reason(report)
            if outcome == "PASS":
                detail = dict(report.user_properties).get("detail", "")
            self.results[report.nodeid] = (outcome, _short(detail))
        elif report.when == "teardown" and report.failed and (
                current is None or current[0] == "PASS"):
            self.results[report.nodeid] = ("ERROR", _short(_reason(report)))

    def rows(self):
        for nodeid, meta in self.meta.items():
            if nodeid in self.results:
                yield meta, *self.results[nodeid]

    def pytest_terminal_summary(self, terminalreporter):
        rows = list(self.rows())
        if not rows:
            return
        s = self.config.stash[SETTINGS]
        tr = terminalreporter
        tr.section(f"Alexa+ MCP checks: {_display_url(s.url)}", sep="=")
        for meta, outcome, detail in rows:
            line = f"{outcome:<5} {meta.get('id', ''):>3}  {meta.get('title', '')}"
            if detail:
                line += f"  -- {detail[:160]}"
            tr.write_line(line, **_markup(outcome))
        counts = {}
        for _, outcome, _ in rows:
            counts[outcome] = counts.get(outcome, 0) + 1
        tr.write_line(", ".join(f"{n} {k.lower()}" for k, n in sorted(counts.items())))

    def pytest_sessionfinish(self, session):
        path = self.config.stash[SETTINGS].summary
        rows = list(self.rows())
        if not path or not rows:
            return
        s = self.config.stash[SETTINGS]
        icon = {"PASS": "pass", "FAIL": "**FAIL**", "ERROR": "**ERROR**",
                "WARN": "warn", "SKIP": "skip"}
        out = [f"## Alexa+ MCP checks: `{_display_url(s.url)}`", "",
               "| # | Check | Level | Result | Detail |", "|---|---|---|---|---|"]
        for meta, outcome, detail in rows:
            cells = [meta.get("id", ""), meta.get("title", ""), meta.get("level", ""),
                     icon.get(outcome, outcome), detail]
            out.append("| " + " | ".join(c.replace("|", "\\|") for c in map(str, cells))
                       + " |")
        out += ["", "Checked by [alexa-plus-mcp-checks]"
                    "(https://github.com/ubmids/alexa-plus-mcp-checks).", ""]
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(out) + "\n")


def _display_url(url: str | None) -> str:
    """The URL without any query string or userinfo, for headings."""
    if not url:
        return "(no URL)"
    p = urllib.parse.urlsplit(url)
    host = p.hostname or ""
    if p.port:
        host += f":{p.port}"
    return urllib.parse.urlunsplit((p.scheme, host, p.path, "", ""))


def _markup(outcome: str) -> dict:
    return {"PASS": {"green": True}, "FAIL": {"red": True, "bold": True},
            "ERROR": {"red": True, "bold": True}, "WARN": {"yellow": True},
            "SKIP": {"yellow": True}}.get(outcome, {})

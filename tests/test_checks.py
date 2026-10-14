"""Every check passes against a conformant server and fails against a server
that breaks its requirement, and only its requirement.

The bundled checks are run through pytester, exactly as a user runs them:
`pytest --alexa-mcp-url URL --alexa-mcp-token TOKEN`.
"""
import os
import subprocess
import sys

import pytest

from alexa_plus_mcp_checks.plugin import parse_tool_call

CHECKS = {
    "1": "test_unauthenticated_request_is_401_without_www_authenticate",
    "2a": "test_protected_resource_metadata_is_served",
    "2b": "test_authorization_server_metadata_advertises_s256",
    "3": "test_bearer_token_in_query_string_is_refused",
    "4": "test_initialize_negotiates_2025_11_25_and_issues_a_session",
    "5a": "test_mcp_apps_views_are_listed_and_served",
    "5b": "test_tool_result_names_its_view",
    "6": "test_warm_round_trip_is_under_the_budget",
    "7": "test_foreign_origin_is_refused_with_403",
    "8": "test_unsupported_protocol_version_is_400",
    "9": "test_missing_session_id_after_initialize_is_400",
    "10": "test_jsonrpc_batch_is_refused",
    "11": "test_bad_arguments_are_a_tool_error_without_internals",
}
TOOL_CALL = ("--alexa-mcp-tool-call", 'show_card={"limit": 2}')


def run(pytester, srv, *extra, token=True):
    args = ["--alexa-mcp-url", srv.url, "--alexa-mcp-latency-samples", "3",
            "-p", "no:cacheprovider"]
    if token:
        args += ["--alexa-mcp-token", srv.token]
    return pytester.inline_run(*args, *extra)


def outcomes(reprec):
    """{check id: passed | failed | skipped | xfailed | error}"""
    by_name = {v: k for k, v in CHECKS.items()}
    out = {}
    for rep in reprec.getreports("pytest_runtest_logreport"):
        name = rep.nodeid.split("::")[-1]
        if rep.when == "call":
            out[by_name.get(name, name)] = ("xfailed" if hasattr(rep, "wasxfail")
                                            else rep.outcome)
        elif rep.when == "setup" and not rep.passed:
            out[by_name.get(name, name)] = "skipped" if rep.skipped else "error"
    return out


def test_a_conformant_server_passes_every_check(pytester, serve):
    got = outcomes(run(pytester, serve(), *TOOL_CALL))
    assert got == {k: "passed" for k in CHECKS}, got


def test_without_a_tool_call_only_the_result_placement_check_is_skipped(pytester, serve):
    got = outcomes(run(pytester, serve()))
    assert got == {**{k: "passed" for k in CHECKS}, "5b": "skipped"}, got


def test_a_server_that_answers_in_sse_is_read_correctly(pytester, serve):
    """Streamable HTTP lets a server answer a POST with text/event-stream."""
    got = outcomes(run(pytester, serve("sse"), *TOOL_CALL))
    assert got == {k: "passed" for k in CHECKS}, got


BAD = [
    ("unauth_403", "1"),
    ("www_authenticate", "1"),
    ("no_prm", "2a"),
    ("prm_wrong_resource", "2a"),
    ("no_as_metadata", "2b"),
    ("no_s256", "2b"),
    ("query_token_ok", "3"),
    ("old_protocol", "4"),
    ("no_session_id", "4"),
    ("ui_wrong_mime", "5a"),
    ("ui_not_listed", "5a"),
    ("ui_result_no_meta", "5b"),
    ("slow", "6"),
    ("origin_ok", "7"),
    ("any_version_ok", "8"),
    ("batch_ok", "10"),
    ("args_protocol_error", "11"),
    ("args_leak", "11"),
]


@pytest.mark.parametrize("flaw,check", BAD, ids=[f"{f}-fails-{c}" for f, c in BAD])
def test_each_flaw_fails_its_check_and_no_other(pytester, serve, flaw, check):
    got = outcomes(run(pytester, serve(flaw), *TOOL_CALL))
    assert got[check] == "failed", got
    others = {k: v for k, v in got.items() if k != check}
    assert "failed" not in others.values() and "error" not in others.values(), got
    if flaw == "no_session_id":
        # A server that issues no session id does not require one.
        assert got["9"] == "skipped", got


def test_the_should_level_session_check_warns_unless_strict(pytester, serve):
    srv = serve("sessionless_ok")
    got = outcomes(run(pytester, srv, *TOOL_CALL))
    assert got["9"] == "xfailed", got
    assert set(got.values()) == {"passed", "xfailed"}, got
    strict = outcomes(run(pytester, srv, *TOOL_CALL, "--alexa-mcp-strict"))
    assert strict["9"] == "failed", strict


def test_bad_arguments_check_picks_a_tool_with_a_required_argument(pytester, serve):
    """Without --alexa-mcp-tool-call it calls `echo` (the first tool with a
    required argument) without that argument."""
    result = run(pytester, serve("args_protocol_error"))
    got = outcomes(result)
    assert got["11"] == "failed", got
    fail = [r for r in result.getreports("pytest_runtest_logreport")
            if r.failed and CHECKS["11"] in r.nodeid][0]
    assert "tools/call echo with `text` missing" in str(fail.longrepr), fail.longrepr
    assert "-32602" in str(fail.longrepr), fail.longrepr


def test_failure_messages_name_the_requirement(pytester, serve):
    result = pytester.runpytest("--alexa-mcp-url", serve("www_authenticate").url,
                                "-p", "no:cacheprovider")
    out = result.stdout.str()
    assert "WWW-Authenticate: Bearer" in out, out
    assert "Alexa+ MCP Toolkit quickstart" in out, out
    assert "FAIL    1  401 without WWW-Authenticate" in out, out


def test_without_a_token_only_the_anonymous_checks_run(pytester, serve):
    got = outcomes(run(pytester, serve(), token=False))
    assert {k for k, v in got.items() if v == "passed"} == {"1", "2a", "2b"}, got
    assert {k for k, v in got.items() if v == "skipped"} == set(CHECKS) - {"1", "2a", "2b"}


def test_the_token_can_come_from_the_environment(pytester, serve, monkeypatch):
    srv = serve()
    monkeypatch.setenv("ALEXA_MCP_TOKEN", srv.token)
    got = outcomes(run(pytester, srv, *TOOL_CALL, token=False))
    assert got == {k: "passed" for k in CHECKS}, got


def test_the_token_is_never_printed(pytester, serve):
    """Not in failure messages, not with locals shown, not anywhere."""
    srv = serve("query_token_ok")
    result = pytester.runpytest("--alexa-mcp-url", srv.url, "--alexa-mcp-token",
                                srv.token, "-p", "no:cacheprovider", "-vv",
                                "--showlocals", "-rA", "--tb=long", *TOOL_CALL)
    out = result.stdout.str() + result.stderr.str()
    assert "FAIL    3" in out, out
    assert srv.token not in out


def test_with_no_url_the_checks_skip_and_say_why(pytester):
    reprec = pytester.inline_run("--pyargs", "alexa_plus_mcp_checks.checks",
                                 "-p", "no:cacheprovider")
    got = outcomes(reprec)
    assert got == {k: "skipped" for k in CHECKS}, got
    skip = reprec.getreports("pytest_runtest_logreport")[0]
    assert "--alexa-mcp-url" in str(skip.longrepr)


def test_the_url_option_runs_only_the_checks_when_no_paths_are_given(pytester, serve):
    pytester.makepyfile(test_mine="def test_mine():\n    pass\n")
    got = outcomes(run(pytester, serve()))
    assert "test_mine" not in got and len(got) == len(CHECKS), got


def test_the_url_option_adds_the_checks_to_explicit_paths(pytester, serve):
    pytester.makepyfile(test_mine="def test_mine():\n    pass\n")
    got = outcomes(run(pytester, serve(), "test_mine.py"))
    assert got["test_mine"] == "passed" and len(got) == len(CHECKS) + 1, got


def test_without_the_url_option_a_projects_own_run_is_untouched(pytester, monkeypatch):
    """ALEXA_MCP_URL in the environment does not hijack an ordinary run."""
    monkeypatch.setenv("ALEXA_MCP_URL", "http://127.0.0.1:9/mcp")
    pytester.makepyfile(test_mine="def test_mine():\n    pass\n")
    got = outcomes(pytester.inline_run("-p", "no:cacheprovider"))
    assert got == {"test_mine": "passed"}, got


def test_a_markdown_summary_is_appended(pytester, serve, tmp_path):
    path = tmp_path / "summary.md"
    path.write_text("# earlier step\n")
    run(pytester, serve("origin_ok"), "--alexa-mcp-summary", str(path))
    md = path.read_text()
    assert md.startswith("# earlier step\n"), md
    assert "| # | Check | Level | Result | Detail |" in md
    assert "| 7 | A foreign Origin is refused with 403 | MUST | **FAIL** |" in md, md
    assert "| 1 | 401 without WWW-Authenticate | MUST | pass |" in md, md


def test_tool_call_parsing():
    assert parse_tool_call('record={"limit": 3}') == ("record", {"limit": 3})
    assert parse_tool_call("household") == ("household", {})
    assert parse_tool_call("household=") == ("household", {})
    for bad in ('x={"a": ', "x=[1]", '={"a": 1}'):
        with pytest.raises(pytest.UsageError):
            parse_tool_call(bad)


def test_a_malformed_tool_call_is_a_usage_error(pytester, serve):
    result = pytester.runpytest("--alexa-mcp-url", serve().url,
                                "--alexa-mcp-tool-call", "record={nope", "-p",
                                "no:cacheprovider")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    assert "not valid JSON" in result.stderr.str()


def cli(*args, token=None, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("ALEXA_MCP_")}
    if token:
        env["ALEXA_MCP_TOKEN"] = token
    return subprocess.run([sys.executable, "-m", "alexa_plus_mcp_checks.cli", *args],
                          capture_output=True, text=True, env=env, cwd=cwd, timeout=120)


def test_the_command_line_tool(serve, tmp_path):
    srv = serve()
    # Run from a directory whose pytest config would break an ordinary run:
    # the command must not pick it up.
    (tmp_path / "pytest.ini").write_text("[pytest]\naddopts = --definitely-not-an-option\n")
    good = cli(srv.url, "--tool-call", 'show_card={"limit": 1}',
               "--latency-samples", "3", "--summary", str(tmp_path / "s.md"),
               token=srv.token, cwd=tmp_path)
    assert good.returncode == 0, good.stdout + good.stderr
    assert "13 passed" in good.stdout, good.stdout
    assert srv.token not in good.stdout + good.stderr
    assert "| 11 |" in (tmp_path / "s.md").read_text()

    bad = cli(serve("batch_ok").url, "--latency-samples", "3", token=srv.token,
              cwd=tmp_path)
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "FAIL   10  A JSON-RPC batch is refused" in bad.stdout, bad.stdout

    missing = cli(cwd=tmp_path)
    assert missing.returncode == 2 and "ALEXA_MCP_URL" in missing.stderr

"""The checks. One test per requirement, each quoting where it comes from.

Two families:

* Alexa+ (1 to 6). What Amazon's Alexa+ MCP toolkit docs require of a
  self-hosted MCP server, concentrating on where that differs from what an
  off-the-shelf MCP SDK does by default. Hearth's friction log
  (github.com/ubmids/hearth, docs/FRICTION_LOG.md) records why each one cost
  time.
* MCP 2025-11-25 Streamable HTTP (7 to 11). Transport rules from the spec that
  the official MCP conformance suite exercises, and that hand-rolled servers
  most often miss.

Run them with `pytest --alexa-mcp-url URL` (token from ALEXA_MCP_TOKEN), with
`alexa-mcp-check URL`, or with `pytest --pyargs alexa_plus_mcp_checks.checks`.
Every check fails with a message saying what was sent, what came back, and
which sentence of which document it breaks.
"""
from __future__ import annotations

import json
import re
import statistics
import time
import urllib.parse

import pytest

from .client import (PROTOCOL_VERSION, McpClient, McpError, excerpt, messages,
                     origin_of, rpc_message)

QUICKSTART = "Alexa+ MCP Toolkit quickstart"
LIFECYCLE = ("Alexa+ MCP Toolkit client lifecycle, https://developer.amazon.com/docs/"
             "alexaplus/add-ons/mcp-toolkit-client-lifecycle.html")
TRANSPORTS = ("MCP 2025-11-25 basic/transports, https://modelcontextprotocol.io/"
              "specification/2025-11-25/basic/transports")
TOOLS_SPEC = ("MCP 2025-11-25 server/tools, error handling (SEP-1303), "
              "https://modelcontextprotocol.io/specification/2025-11-25/server/tools")
APP_MIME = "text/html;profile=mcp-app"
FOREIGN_ORIGIN = "http://evil.example.com"
BAD_VERSION = "1999-01-01"
UNKNOWN_ARG = "alexa_mcp_checks_unknown_argument"


def check(id, title, level, source):
    return pytest.mark.alexa_mcp(id=id, title=title, level=level, source=source)


def require(ok, message):
    """A failure that reads as a sentence, without pytest's introspection
    (which would print response objects, and the URL inside them)."""
    if not ok:
        pytest.fail(message, pytrace=False)


def ping(client: McpClient) -> dict:
    return client.request_body("ping")


def ui_uri(obj) -> str | None:
    """`_meta.ui.resourceUri` on a tool definition or a tool result."""
    meta = obj.get("_meta") if isinstance(obj, dict) else None
    ui = meta.get("ui") if isinstance(meta, dict) else None
    uri = ui.get("resourceUri") if isinstance(ui, dict) else None
    return uri if isinstance(uri, str) and uri else None


def result_text(result: dict) -> str:
    parts = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


# --- Alexa+ ---------------------------------------------------------------

@check("1", "401 without WWW-Authenticate", "MUST", QUICKSTART)
def test_unauthenticated_request_is_401_without_www_authenticate(
        alexa_mcp_anon, record_property):
    """Alexa+ MCP Toolkit quickstart: "Your MCP server returns `401
    Unauthorized` (without a `WWW-Authenticate` header) for unauthenticated
    requests."

    The trap: RFC 9728 and the MCP authorization spec say to send that header,
    and SDKs add it for you. Checked with no Authorization header at all and
    with a bearer token the server cannot know.
    """
    c = alexa_mcp_anon
    for label, headers in (("no Authorization header", {}),
                           ("an unknown bearer token",
                            {"Authorization": "Bearer alexa-mcp-checks-not-a-real-token"})):
        r = c.post(c.request_body("initialize", {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
            "clientInfo": {"name": "alexa-plus-mcp-checks", "version": "0"}}),
            headers=headers)
        require(r.status_code == 401,
                f"POST /mcp with {label} answered {c.excerpt(r)}; Alexa+ expects "
                f"401 Unauthorized for an unauthenticated request ({QUICKSTART}).")
        www = r.headers.get("www-authenticate")
        require(www is None,
                f"the 401 for {label} carries `WWW-Authenticate: {www}`. Alexa+ "
                f"requires the 401 *without* it ({QUICKSTART}), even though RFC "
                f"9728 and most MCP SDKs add it by default. Remove the header.")
    record_property("detail", "401, no WWW-Authenticate, with no token and a bad token")


def _get_json(client: McpClient, url: str):
    try:
        r = client.http.get(url, headers={"Accept": "application/json"})
    except Exception as e:  # noqa: BLE001 - reported, not raised
        return None, f"{url}: {type(e).__name__}"
    if r.status_code != 200:
        return None, f"{url}: {excerpt(r, limit=120)}"
    try:
        body = r.json()
    except ValueError:
        return None, f"{url}: 200 but not JSON"
    if not isinstance(body, dict):
        return None, f"{url}: 200 but not a JSON object"
    return body, None


def _well_known(base: str, name: str) -> list[str]:
    """RFC 8414 / RFC 9728 location: the well-known segment goes between the
    host and the path. A path-less form is tried as well, since that is the
    one the Alexa+ docs spell out and the one most servers serve."""
    p = urllib.parse.urlsplit(base)
    root = f"{p.scheme}://{p.netloc}/.well-known/{name}"
    path = p.path.rstrip("/")
    return [root + path, root] if path else [root]


def _prm(client: McpClient):
    tried = []
    for url in _well_known(client.url, "oauth-protected-resource"):
        body, err = _get_json(client, url)
        if body is not None:
            return url, body, tried
        tried.append(err)
    return None, None, tried


@check("2a", "Protected resource metadata (RFC 9728)", "MUST", QUICKSTART)
def test_protected_resource_metadata_is_served(alexa_mcp_anon, record_property):
    """Alexa+ MCP Toolkit quickstart: serve "a Protected Resource Metadata
    (PRM) document at the well-known URI according to RFC 9728". The path is
    only given by reference, which is the trap: the quickstart spells out the
    authorization server path literally and not this one.

    Looks at /.well-known/oauth-protected-resource/<path> and then
    /.well-known/oauth-protected-resource. RFC 9728 section 2 makes `resource`
    required and section 3.3 says it must be the resource the client asked
    about, which is the URL under test. `authorization_servers` is how Alexa+
    finds the authorization server.
    """
    c = alexa_mcp_anon
    url, prm, tried = _prm(c)
    require(prm is not None,
            "no protected resource metadata document. Tried "
            + "; ".join(tried) + f". Alexa+ requires one at the RFC 9728 "
            f"well-known URI ({QUICKSTART}).")
    resource = prm.get("resource")
    require(isinstance(resource, str) and resource,
            f"{url} has no `resource` (RFC 9728 section 2 makes it required): "
            f"{json.dumps(prm)[:300]}")
    require(resource.rstrip("/") == c.url.rstrip("/"),
            f"{url} says resource is {resource!r} but the server was reached at "
            f"{c.url!r}. RFC 9728 section 3.3: they must be identical, and Alexa+ "
            f"sends this value as the RFC 8707 `resource` when it asks for a token.")
    servers = prm.get("authorization_servers")
    require(isinstance(servers, list) and servers
            and all(isinstance(s, str) and s for s in servers),
            f"{url} has no `authorization_servers` list, so a client cannot find "
            f"where to authorize: {json.dumps(prm)[:300]}")
    record_property("detail", f"{urllib.parse.urlsplit(url).path}; resource "
                              f"{resource}; authorization_servers {servers}")


@check("2b", "Authorization server metadata advertises S256", "MUST", QUICKSTART)
def test_authorization_server_metadata_advertises_s256(alexa_mcp_anon, record_property):
    """Alexa+ MCP Toolkit quickstart: serve the RFC 8414 document at
    `/.well-known/oauth-authorization-server`, with `S256` present in
    `code_challenge_methods_supported`.

    RFC 8414 lets a server leave that field out, and an absent field is not
    the same as S256. Alexa+ asks for it to be present. The document is
    looked up for the first `authorization_servers` entry of the protected
    resource metadata, falling back to the MCP server's own origin.
    """
    c = alexa_mcp_anon
    _, prm, _ = _prm(c)
    bases = []
    if prm and isinstance(prm.get("authorization_servers"), list):
        bases += [s for s in prm["authorization_servers"][:1] if isinstance(s, str)]
    bases.append(origin_of(c.url))
    candidates = list(dict.fromkeys(u for b in bases
                                    for u in _well_known(b, "oauth-authorization-server")))
    meta, url, tried = None, None, []
    for u in candidates:
        meta, err = _get_json(c, u)
        if meta is not None:
            url = u
            break
        tried.append(err)
    require(meta is not None,
            "no authorization server metadata document. Tried " + "; ".join(tried)
            + f". Alexa+ requires /.well-known/oauth-authorization-server ({QUICKSTART}).")
    require("code_challenge_methods_supported" in meta,
            f"{url} has no `code_challenge_methods_supported`. Alexa+ requires it "
            f"to be present and to list S256 ({QUICKSTART}); RFC 8414 lets it be "
            f"omitted, which is exactly the default that fails here.")
    methods = meta["code_challenge_methods_supported"]
    require(isinstance(methods, list) and "S256" in methods,
            f"{url}: code_challenge_methods_supported is {methods!r}; it must "
            f"include \"S256\" ({QUICKSTART}).")
    record_property("detail", f"{urllib.parse.urlsplit(url).path}; "
                              f"code_challenge_methods_supported {methods}")


@check("3", "Bearer token in the query string is refused", "MUST", QUICKSTART)
def test_bearer_token_in_query_string_is_refused(alexa_mcp_client, record_property):
    """Alexa+ MCP Toolkit quickstart: "Don't send the Bearer token in the query
    string." OAuth 2.1 drops the query-string method entirely, because tokens
    there end up in logs, referrers and browser history.

    A server that quietly accepts `?access_token=` is accepting exactly what
    the requirement forbids. Sent twice: alongside a valid Authorization
    header (the request is otherwise perfect, so only the query string can be
    the reason for refusing it), and on its own.
    """
    c = alexa_mcp_client
    r = c.post(ping(c), token_in_query=True)
    require(400 <= r.status_code < 500,
            f"POST /mcp?access_token=<token> with a valid Authorization header and "
            f"session was answered {c.excerpt(r)}. A token in the query string must "
            f"be refused with a 4xx, not used ({QUICKSTART}).")
    alone = c.post(ping(c), auth=False, token_in_query=True)
    require(400 <= alone.status_code < 500,
            f"POST /mcp?access_token=<token> with no Authorization header was "
            f"answered {c.excerpt(alone)}: the server authenticated a request from "
            f"a token in the query string ({QUICKSTART}).")
    record_property("detail", f"refused with {r.status_code} (with header) and "
                              f"{alone.status_code} (query only)")


@check("4", "initialize negotiates 2025-11-25 and issues Mcp-Session-Id", "MUST",
       QUICKSTART)
def test_initialize_negotiates_2025_11_25_and_issues_a_session(
        alexa_mcp_settings, record_property):
    """The Alexa+ MCP Toolkit quickstart requires MCP spec version 2025-11-25
    over Streamable HTTP. (The client lifecycle page's example still shows
    2025-03-26, which is the trap: copy it and you ship the wrong version.)

    initialize is sent asking for 2025-11-25, and the server must answer with
    2025-11-25 and an `Mcp-Session-Id` header, which Streamable HTTP uses to
    tie the rest of the conversation to this initialization.
    """
    s = alexa_mcp_settings
    if not s.token:
        pytest.skip("needs a bearer token: pass --alexa-mcp-token or set ALEXA_MCP_TOKEN")
    c = McpClient(s.url, s.token, s.timeout)
    try:
        r = c.initialize(PROTOCOL_VERSION)
        require(r.status_code == 200,
                f"initialize was answered {c.excerpt(r)}; expected 200 with a result.")
        msg = rpc_message(r)
        require(isinstance(msg, dict) and isinstance(msg.get("result"), dict),
                f"initialize returned no JSON-RPC result: {c.excerpt(r)}")
        version = msg["result"].get("protocolVersion")
        require(version == PROTOCOL_VERSION,
                f"initialize asked for {PROTOCOL_VERSION} and the server answered "
                f"protocolVersion {version!r}. Alexa+ requires 2025-11-25 ({QUICKSTART}).")
        sid = r.headers.get("mcp-session-id")
        require(bool(sid),
                "initialize issued no Mcp-Session-Id header. Streamable HTTP keeps a "
                f"conversation together with it ({TRANSPORTS}, session management).")
        require(all(0x21 <= ord(ch) <= 0x7E for ch in sid),
                f"Mcp-Session-Id must be visible ASCII only (0x21 to 0x7E), got {sid!r} "
                f"({TRANSPORTS}).")
    finally:
        c.close()
    record_property("detail", f"protocolVersion {version}, session id issued")


@check("5a", "MCP Apps views are listed and served as text/html;profile=mcp-app",
       "MUST", "Alexa+ quickstart: follow the MCP Apps standard (SEP-1865)")
def test_mcp_apps_views_are_listed_and_served(alexa_mcp_client, alexa_mcp_tools,
                                              record_property):
    """Alexa+ MCP Toolkit quickstart: "If you expect to provide visuals, follow
    the MCP Apps standard." In MCP Apps a tool names its view in
    `_meta.ui.resourceUri`, the view is a `ui://` resource, and
    `resources/read` serves it with mimeType `text/html;profile=mcp-app`.

    Skipped when no tool declares a view. Every declared view must start with
    ui://, appear in resources/list, and read back as an MCP App with content.
    """
    c = alexa_mcp_client
    declared = {}
    for t in alexa_mcp_tools:
        uri = ui_uri(t)
        if uri:
            declared.setdefault(uri, []).append(t.get("name"))
    if not declared:
        pytest.skip("no tool declares _meta.ui.resourceUri, so there is no MCP App to check")

    problems = []
    listed = {r.get("uri") for r in c.list_all("resources/list", "resources")
              if isinstance(r, dict)}
    for uri, names in declared.items():
        who = ", ".join(map(str, names))
        if not uri.startswith("ui://"):
            problems.append(f"{who}: resourceUri {uri!r} is not a ui:// URI")
            continue
        if uri not in listed:
            problems.append(f"{who}: {uri} is not in resources/list")
        try:
            read = c.call("resources/read", {"uri": uri})
        except McpError as e:
            problems.append(f"{who}: resources/read {uri} failed: {e}")
            continue
        contents = [x for x in read.get("contents") or [] if isinstance(x, dict)]
        match = [x for x in contents if x.get("uri") == uri] or contents
        if not match:
            problems.append(f"{who}: resources/read {uri} returned no contents")
            continue
        mime = match[0].get("mimeType")
        if mime != APP_MIME:
            problems.append(f"{who}: {uri} is served as {mime!r}, not {APP_MIME!r}")
        if not (match[0].get("text") or match[0].get("blob")):
            problems.append(f"{who}: {uri} has no text or blob")
    require(not problems,
            "MCP Apps views are not served the way the MCP Apps standard says:\n  "
            + "\n  ".join(problems))
    record_property("detail", f"{len(declared)} view(s) for "
                              f"{sum(len(v) for v in declared.values())} tool(s): "
                              + ", ".join(sorted(declared)))


@check("5b", "A tool result with structuredContent names its view", "MUST", LIFECYCLE)
def test_tool_result_names_its_view(alexa_mcp_settings, alexa_mcp_client,
                                    alexa_mcp_tools, record_property):
    """Alexa+ client lifecycle page: "Alexa+ renders your custom visuals for
    your MCP App UI as long as you have resourceUri defined in the tool
    response", and its example puts `_meta.ui.resourceUri` on the *result*.
    The MCP Apps spec puts it on the tool definition. Hearth's friction log
    entry 6 records the disagreement; the safe answer is both.

    Needs a real call: `--alexa-mcp-tool-call NAME='{"json": "args"}'`. For a
    tool that declares a view, a result carrying `structuredContent` must also
    carry `_meta.ui.resourceUri`. A tool without a view is not judged: Alexa+
    renders its structuredContent natively.
    """
    s, c = alexa_mcp_settings, alexa_mcp_client
    if not s.tool_calls:
        pytest.skip("pass --alexa-mcp-tool-call NAME='{\"arg\": 1}' to check where a "
                    "real tool result names its view")
    by_name = {t.get("name"): t for t in alexa_mcp_tools}
    problems, seen, judged = [], [], 0
    for name, args in s.tool_calls:
        if name not in by_name:
            problems.append(f"{name}: not in tools/list")
            continue
        r, msg = c.call_raw("tools/call", {"name": name, "arguments": args})
        if r.status_code != 200 or not isinstance(msg, dict) or "result" not in msg:
            problems.append(f"{name}: the call did not return a result: {c.excerpt(r)}")
            continue
        result = msg["result"]
        if result.get("isError"):
            problems.append(f"{name}: the call failed (isError), so where its result "
                            f"names a view cannot be checked: "
                            f"{c.redact(result_text(result))[:200]}")
            continue
        declared = ui_uri(by_name[name])
        if "structuredContent" not in result:
            seen.append(f"{name}: no structuredContent")
            continue
        if not declared:
            seen.append(f"{name}: tool declares no view")
            continue
        judged += 1
        got = ui_uri(result)
        if not got:
            problems.append(
                f"{name}: the result has structuredContent but no _meta.ui.resourceUri. "
                f"The tool declares {declared}, and Alexa+ reads the view from the "
                f"tool response ({LIFECYCLE}).")
        elif not got.startswith("ui://"):
            problems.append(f"{name}: result _meta.ui.resourceUri {got!r} is not ui://")
        else:
            seen.append(f"{name}: result names {got}"
                        + ("" if got == declared else f" (definition names {declared})"))
    require(not problems, "\n  ".join(["tool results do not name their view:"] + problems))
    if not judged:
        pytest.skip("nothing to judge: " + "; ".join(seen))
    record_property("detail", "; ".join(seen))


@check("6", "Warm round trip under the latency budget", "MUST",
       "Alexa+ add-on requirements: round trip under 500 ms")
def test_warm_round_trip_is_under_the_budget(alexa_mcp_settings, alexa_mcp_client,
                                             record_property):
    """Alexa+: "Your MCP server must meet a round-trip query response latency
    of less than 500 ms."

    The median of N tools/list calls over one kept-alive connection, after
    two warm-up calls. It is measured from wherever this runs, and Alexa+
    does not say where it measures from (Hearth friction log entry 2): from a
    machine far from your server the network alone can use most of the
    budget, so run this from close to where Alexa+ calls you (AWS us-east-1
    is a reasonable stand-in) before concluding anything. Tune with
    --alexa-mcp-latency-samples and --alexa-mcp-latency-budget-ms.
    """
    s, c = alexa_mcp_settings, alexa_mcp_client
    for _ in range(2):
        c.call("tools/list")
    times = []
    for _ in range(s.samples):
        t0 = time.perf_counter()
        r = c.post(c.request_body("tools/list"))
        times.append((time.perf_counter() - t0) * 1000)
        require(r.status_code == 200, f"tools/list answered {c.excerpt(r)} while timing")
    med = statistics.median(times)
    summary = (f"median {med:.1f} ms, min {min(times):.1f}, max {max(times):.1f} "
               f"over {len(times)} tools/list calls (budget {s.budget_ms:g} ms)")
    require(med < s.budget_ms,
            f"{summary}. Alexa+ requires a round trip under 500 ms. If the server's "
            f"own time is small, the network from here may be the cause: measure "
            f"from near AWS us-east-1 before changing the server.")
    record_property("detail", summary)


# --- MCP 2025-11-25 Streamable HTTP ---------------------------------------

@check("7", "A foreign Origin is refused with 403", "MUST", TRANSPORTS)
def test_foreign_origin_is_refused_with_403(alexa_mcp_client, record_property):
    """MCP 2025-11-25 transports, security warning: "Servers MUST validate the
    Origin header on all incoming connections to prevent DNS rebinding
    attacks. If the Origin header is present and invalid, servers MUST
    respond with HTTP 403 Forbidden."

    An otherwise valid request (token, session, version) with
    `Origin: http://evil.example.com`. Alexa+ itself sends no Origin, so a
    server can refuse foreign ones without affecting it.
    """
    c = alexa_mcp_client
    r = c.post(ping(c), headers={"Origin": FOREIGN_ORIGIN})
    require(r.status_code == 403,
            f"a request with Origin: {FOREIGN_ORIGIN} was answered {c.excerpt(r)}. "
            f"A present, invalid Origin MUST get 403 Forbidden ({TRANSPORTS}).")
    record_property("detail", f"Origin {FOREIGN_ORIGIN} refused with 403")


@check("8", "An unsupported MCP-Protocol-Version is 400", "MUST", TRANSPORTS)
def test_unsupported_protocol_version_is_400(alexa_mcp_client, record_property):
    """MCP 2025-11-25 transports, protocol version header: "If the server
    receives a request with an invalid or unsupported MCP-Protocol-Version,
    it MUST respond with 400 Bad Request."

    An otherwise valid ping carrying `MCP-Protocol-Version: 1999-01-01`.
    """
    c = alexa_mcp_client
    r = c.post(ping(c), headers={"MCP-Protocol-Version": BAD_VERSION})
    require(r.status_code == 400,
            f"a ping with MCP-Protocol-Version: {BAD_VERSION} was answered "
            f"{c.excerpt(r)}. An unsupported version MUST get 400 Bad Request "
            f"({TRANSPORTS}).")
    record_property("detail", f"MCP-Protocol-Version {BAD_VERSION} refused with 400")


@check("9", "A request without Mcp-Session-Id after initialize is 400", "SHOULD",
       TRANSPORTS)
def test_missing_session_id_after_initialize_is_400(alexa_mcp_settings, alexa_mcp_client,
                                                    record_property):
    """MCP 2025-11-25 transports, session management: "Servers that require a
    session ID SHOULD respond to requests without an Mcp-Session-Id header
    (other than initialization) with HTTP 400 Bad Request."

    Applies to servers that issue a session id on initialize. A SHOULD, so a
    violation is reported as a warning (xfail) unless --alexa-mcp-strict is
    given, in which case it fails.
    """
    c = alexa_mcp_client
    if not c.session_id:
        pytest.skip("the server issued no Mcp-Session-Id, so it does not require one")
    r = c.post(ping(c), session=False)
    if r.status_code != 400:
        message = (f"a ping without Mcp-Session-Id, after initialize issued one, was "
                   f"answered {c.excerpt(r)}. A server that issues session ids SHOULD "
                   f"answer such a request with 400 Bad Request ({TRANSPORTS}).")
        if alexa_mcp_settings.strict:
            pytest.fail(message, pytrace=False)
        pytest.xfail("SHOULD-level, reported as a warning (--alexa-mcp-strict fails "
                     "it): " + message)
    record_property("detail", "request without a session id refused with 400")


@check("10", "A JSON-RPC batch is refused", "MUST", TRANSPORTS)
def test_jsonrpc_batch_is_refused(alexa_mcp_client, record_property):
    """MCP 2025-11-25 transports: "The body of the POST request MUST be a
    single JSON-RPC request, notification, or response." Batching was removed
    from MCP in 2025-06-18.

    A JSON array of two pings. Refusing means a 4xx status, or a JSON-RPC
    -32600 Invalid Request; answering the pings is a failure.
    """
    c = alexa_mcp_client
    batch = [ping(c), ping(c)]
    r = c.post(batch)
    if 400 <= r.status_code < 500:
        record_property("detail", f"batch refused with HTTP {r.status_code}")
        return
    found = messages(r)
    body = found[-1] if found else None

    def invalid(m):
        return isinstance(m, dict) and isinstance(m.get("error"), dict) \
            and m["error"].get("code") == -32600

    refused = invalid(body) or (isinstance(body, list) and body
                                and all(invalid(m) for m in body))
    require(refused,
            f"a JSON-RPC batch of two pings was answered {c.excerpt(r)}. A 2025-11-25 "
            f"POST body is a single message, so a batch must be refused with a 4xx "
            f"or -32600 Invalid Request ({TRANSPORTS}).")
    record_property("detail", f"batch refused with -32600 (HTTP {r.status_code})")


LEAKS = [
    (r"Traceback \(most recent call last\)", "a Python traceback"),
    (r'File "[^"]+", line \d+', "a Python stack frame"),
    (r"\w+\(\) (got an unexpected keyword argument|missing \d+ required)",
     "a Python function signature error"),
    (r"takes \d+ positional arguments? but \d+ (were|was) given",
     "a Python function signature error"),
    (r"object has no attribute", "a Python AttributeError"),
    (r"\b(TypeError|KeyError|AttributeError|NameError|IndexError):", "a Python exception"),
    (r"\bat \S+ \(\S+\.(js|mjs|cjs|ts):\d+:\d+\)", "a JavaScript stack frame"),
]


def _bad_call(settings, tools):
    """The tool and arguments to send, and whether they are certainly invalid.

    Starts from the user's real call when there is one, otherwise from the
    first tool with a required argument. Drops one required argument (which
    makes the call invalid by any reading of the schema) and adds an unknown
    one.
    """
    by_name = {t.get("name"): t for t in tools}
    if settings.tool_calls and settings.tool_calls[0][0] in by_name:
        name, args = settings.tool_calls[0]
        tool = by_name[name]
    else:
        def required(t):
            schema = t.get("inputSchema") or {}
            return schema.get("required") or []
        tool = next((t for t in tools if required(t)), tools[0])
        name, args = tool.get("name"), {}
    schema = tool.get("inputSchema") or {}
    args = dict(args)
    required = [r for r in schema.get("required") or [] if isinstance(r, str)]
    present = [r for r in required if r in args]
    dropped = present[0] if present else (required[0] if required else None)
    args.pop(dropped, None)
    args[UNKNOWN_ARG] = 1
    certain = dropped is not None or schema.get("additionalProperties") is False
    return name, args, dropped, certain


@check("11", "Bad tool arguments are an isError result that leaks no internals",
       "MUST", TOOLS_SPEC)
def test_bad_arguments_are_a_tool_error_without_internals(alexa_mcp_settings,
                                                          alexa_mcp_client,
                                                          alexa_mcp_tools,
                                                          record_property):
    """MCP 2025-11-25 (SEP-1303) treats input validation failures as tool
    execution errors: a normal result with `isError: true` that the model can
    read and correct, not a -32602 protocol error that ends the call. And what
    the model reads is spoken to a person by Alexa+, so it must not be a stack
    trace or a function signature.

    The call is the --alexa-mcp-tool-call one if given (else the first tool
    with a required argument), with one required argument removed and an
    unknown argument added.
    """
    s, c, tools = alexa_mcp_settings, alexa_mcp_client, alexa_mcp_tools
    if not tools:
        pytest.skip("the server lists no tools")
    name, args, dropped, certain = _bad_call(s, tools)
    what = (f"tools/call {name} with {'`' + dropped + '` missing and ' if dropped else ''}"
            f"an unknown argument `{UNKNOWN_ARG}`")
    r, msg = c.call_raw("tools/call", {"name": name, "arguments": args})
    require(r.status_code == 200 and isinstance(msg, dict),
            f"{what} was answered {c.excerpt(r)}; expected 200 with a tool result "
            f"carrying isError ({TOOLS_SPEC}).")

    def leaks(text):
        return [label for pattern, label in LEAKS if re.search(pattern, text)]

    if "error" in msg:
        err = msg["error"] if isinstance(msg["error"], dict) else {}
        text = c.redact(str(err.get("message", "")))
        leaked = leaks(text + json.dumps(err.get("data", "")))
        pytest.fail(f"{what} was answered with JSON-RPC error {err.get('code')} "
                    f"{text[:200]!r}. Invalid arguments are a tool execution error: a "
                    f"result with isError: true ({TOOLS_SPEC})."
                    + (f" The message also exposes {', '.join(dict.fromkeys(leaked))}."
                       if leaked else ""), pytrace=False)
    result = msg.get("result")
    require(isinstance(result, dict), f"{what} returned no result: {c.excerpt(r)}")
    text = result_text(result)
    if result.get("isError") is not True:
        if not certain:
            pytest.skip(f"{what} succeeded. Its schema has no required argument to drop "
                        f"and does not set additionalProperties: false, so JSON Schema "
                        f"allows the extra argument; pass --alexa-mcp-tool-call for a "
                        f"tool with a required argument to check this")
        pytest.fail(f"{what} did not come back with isError: true (got "
                    f"isError={result.get('isError')!r}, text {c.redact(text)[:200]!r}). "
                    f"The server accepted arguments its own schema rules out "
                    f"({TOOLS_SPEC}).", pytrace=False)
    require(text.strip(), f"{what}: the isError result has no text saying what was wrong, "
                          f"so the model has nothing to correct itself from.")
    leaked = leaks(text + json.dumps(result.get("structuredContent", "")))
    require(not leaked,
            f"{what}: the isError text exposes {', '.join(dict.fromkeys(leaked))}: "
            f"{c.redact(text)[:300]!r}. Say what was wrong with the arguments in plain "
            f"words; Alexa+ may read it aloud.")
    record_property("detail", f"{name}: isError, {c.redact(text)[:120]!r}")

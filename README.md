# alexa-plus-mcp-checks

A pytest plugin and GitHub Action that check a self-hosted MCP server against
what Amazon's Alexa+ MCP toolkit docs require, concentrating on the places
where that differs from what a general-purpose MCP SDK does by default, plus
the MCP 2025-11-25 Streamable HTTP transport rules that the official MCP
conformance suite exercises.

It came out of building [Hearth](https://github.com/ubmids/hearth), an Alexa+
add-on with a hand-rolled MCP server. Every Alexa+ check below cost Hearth time
at least once, and its friction log (`docs/FRICTION_LOG.md` in the Hearth
repository) records how. The add-on tools and the Alexa+ Web Simulator are available to
select partners only, so most developers have no way to find out whether a
server would connect. Most of what needs checking is protocol, though, and
checking protocol does not need access to Alexa+.

## What it checks, and why

Each check is one pytest test with a docstring quoting its source. A failure
says what was sent, what came back, and which requirement it breaks.

### Alexa+

| # | Check | Why it is here |
|---|---|---|
| 1 | An unauthenticated `POST /mcp` gets `401` **without** `WWW-Authenticate` | The Alexa+ MCP Toolkit quickstart: "Your MCP server returns `401 Unauthorized` (without a `WWW-Authenticate` header) for unauthenticated requests." RFC 9728 and the MCP authorization spec say the opposite, and SDKs add the header for you. Tested with no token and with an unknown one. |
| 2a | Protected Resource Metadata (RFC 9728) is served, with `resource` and `authorization_servers` | The quickstart asks for "a Protected Resource Metadata (PRM) document at the well-known URI according to RFC 9728" but never writes out the path. The check tries `/.well-known/oauth-protected-resource/<path>` and then `/.well-known/oauth-protected-resource`. `resource` must match the URL under test (RFC 9728 section 3.3), because Alexa+ sends it back as the RFC 8707 `resource`. |
| 2b | Authorization Server Metadata (RFC 8414) lists `S256` in `code_challenge_methods_supported` | The quickstart names `/.well-known/oauth-authorization-server` and requires `S256` to be present. RFC 8414 lets a server leave the field out, and leaving it out is the default that fails. |
| 3 | A bearer token in the query string (`?access_token=`) is refused with a 4xx | The quickstart: "Don't send the Bearer token in the query string." A server that quietly accepts it is accepting exactly that. Sent alongside a valid `Authorization` header, so the query string is the only thing wrong with the request, and then on its own. |
| 4 | `initialize` negotiates `2025-11-25` and issues `Mcp-Session-Id` | The quickstart requires MCP 2025-11-25 over Streamable HTTP. The [client lifecycle](https://developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-client-lifecycle.html) example still shows `2025-03-26`, so copying the example ships the wrong version. |
| 5a | Every `_meta.ui.resourceUri` on a tool is a `ui://` resource that `resources/list` lists and `resources/read` serves as `text/html;profile=mcp-app` | The quickstart: "If you expect to provide visuals, follow the MCP Apps standard." Skipped if no tool declares a view. |
| 5b | A tool result with `structuredContent` also carries `_meta.ui.resourceUri` | The client lifecycle page: Alexa+ renders your view "as long as you have resourceUri defined in the tool response", and its example puts it on the *result*. The MCP Apps spec puts it on the tool definition (Hearth friction log, entry 6). The safe choice is to set both. This check needs a real call (`--alexa-mcp-tool-call`). It only judges tools that declare a view. |
| 6 | Warm round trip under 500 ms | Alexa+: "Your MCP server must meet a round-trip query response latency of less than 500 ms." The check takes the median of N `tools/list` calls over one kept-alive connection. Alexa+ does not say where it measures from, so a slow result from a machine far from the server may be the network's fault (friction log, entry 2). Run it from near AWS us-east-1 before concluding anything. |

### MCP 2025-11-25 Streamable HTTP

| # | Check | Source |
|---|---|---|
| 7 | A foreign `Origin` gets `403` | [Transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports): "If the Origin header is present and invalid, servers MUST respond with HTTP 403 Forbidden." Alexa+ sends no `Origin`, so refusing foreign ones costs nothing. |
| 8 | An unsupported `MCP-Protocol-Version` gets `400` | Transports: "If the server receives a request with an invalid or unsupported MCP-Protocol-Version, it MUST respond with 400 Bad Request." |
| 9 | A request without `Mcp-Session-Id` after `initialize` gets `400` (SHOULD) | Transports: "Servers that require a session ID SHOULD respond to requests without an Mcp-Session-Id header (other than initialization) with HTTP 400 Bad Request." This is a SHOULD, so a violation shows as a warning (`xfail`). `--alexa-mcp-strict` turns it into a failure. Skipped if the server issues no session ids. |
| 10 | A JSON-RPC batch (array body) is refused, with a 4xx or `-32600` | Transports: the POST body "MUST be a single JSON-RPC request, notification, or response". Batching was removed in 2025-06-18. |
| 11 | Bad tool arguments come back as a result with `isError: true`, not a `-32602` protocol error, and with no stack trace or function signature in the text | [Tools, error handling](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) (SEP-1303): input validation errors are tool execution errors the model can read and correct. Alexa+ may read that text aloud. The check uses your `--alexa-mcp-tool-call` (or the first tool with a required argument), drops one required argument, and adds an unknown one. |

Not checked: the OAuth authorization code flow itself (a client cannot drive
another server's login), and `Host` header validation (behind a hosting
platform a foreign `Host` is usually routed away before it reaches the server,
so the result would describe the platform rather than the server).

## Install

It is not on PyPI or the GitHub Marketplace. Install it from the repository,
which is where it will live:

```sh
pip install git+https://github.com/ubmids/alexa-plus-mcp-checks
```

Python 3.10 or newer. The only dependencies are `pytest` (7.4 or newer) and
`httpx`.

## Usage with pytest

```sh
export ALEXA_MCP_TOKEN=...            # keeps the token out of shell history and ps
pytest --alexa-mcp-url https://your-host.example/mcp
```

Passing `--alexa-mcp-url` on the command line adds the bundled checks to the
run. If you give no paths, only the checks run, not your project's own tests.
If you give paths, the checks run as well as those paths. Setting
`ALEXA_MCP_URL` on its own never adds the checks to an ordinary `pytest` run.
It only supplies the URL to the two explicit forms:

```sh
pytest --pyargs alexa_plus_mcp_checks.checks      # URL from ALEXA_MCP_URL
alexa-mcp-check https://your-host.example/mcp      # see below
```

Options (each also has an environment variable):

| Option | Env | Meaning |
|---|---|---|
| `--alexa-mcp-url URL` | `ALEXA_MCP_URL` | The MCP endpoint. |
| `--alexa-mcp-token TOKEN` | `ALEXA_MCP_TOKEN` | Bearer token. Without one, only checks 1, 2a and 2b run, and the rest skip. |
| `--alexa-mcp-tool-call NAME=JSON` | `ALEXA_MCP_TOOL_CALL` (one per line) | A real call, e.g. `record='{"limit": 3}'`. Needed for 5b, and used as the base for 11. Pick a read-only tool. |
| `--alexa-mcp-strict` | `ALEXA_MCP_STRICT=1` | SHOULD-level checks fail instead of warning. |
| `--alexa-mcp-latency-budget-ms MS` | `ALEXA_MCP_LATENCY_BUDGET_MS` | Default 500. |
| `--alexa-mcp-latency-samples N` | `ALEXA_MCP_LATENCY_SAMPLES` | Default 10. |
| `--alexa-mcp-timeout SECONDS` | `ALEXA_MCP_TIMEOUT` | Per request. Default 10. |
| `--alexa-mcp-summary PATH` | | Append a markdown results table to PATH. |

At the end of the run a table lists every check as PASS, FAIL, WARN or SKIP,
with a one-line reason. The token never appears in any output. The test suite
checks this with `-vv --showlocals`.

## The `alexa-mcp-check` command

```sh
ALEXA_MCP_TOKEN=... alexa-mcp-check https://your-host.example/mcp \
    --tool-call 'record={"limit": 3}' [--strict] [--summary out.md] [-- extra pytest args]
```

This runs only the bundled checks. It uses the package's own empty ini file, so
the pytest configuration of the directory you run it from cannot change what
is checked. The exit status is pytest's: 0 when everything passed.

## GitHub Action

```yaml
- uses: ubmids/alexa-plus-mcp-checks@main
  with:
    url: https://your-host.example/mcp
    token: ${{ secrets.MCP_TOKEN }}
    tool-call: 'record={"limit": 3}'   # optional
    strict: "false"                     # optional
```

The action is composite. It sets up Python, installs the action's own
checkout, runs `alexa-mcp-check` and appends the results table to the job
summary. Inputs reach the script through environment variables, never through
the script text, and the token is masked. Remember that check 6 then measures
from a GitHub runner. That is usually closer to AWS than a laptop is, but it is
still not where Alexa+ sits.

## Running against a local server

```sh
# start your server, e.g. on 127.0.0.1:8000, and get a token for it
export ALEXA_MCP_TOKEN=...
pytest --alexa-mcp-url http://127.0.0.1:8000/mcp -v
```

For check 2a to pass, the server's protected resource metadata must name the
URL you test: start the server with the matching public URL (for Hearth,
`HEARTH_ISSUER=http://127.0.0.1:8000`).

## Results against Hearth

Run on 2026-10-14 over loopback, with a token from Hearth's local-only
`/api/console-token` and `--alexa-mcp-tool-call 'record={"limit": 3}'`:

| # | Check | Hearth now | Hearth before its transport fix (4fcf406) |
|---|---|---|---|
| 1 | 401 without WWW-Authenticate | pass | pass |
| 2a | Protected resource metadata | pass | pass |
| 2b | AS metadata advertises S256 | pass | pass |
| 3 | Query-string token refused | pass | pass |
| 4 | 2025-11-25 and Mcp-Session-Id | pass | pass |
| 5a | MCP Apps view served | pass | pass |
| 5b | Result names its view | pass | pass |
| 6 | Warm round trip < 500 ms | pass (0.6 ms median, loopback) | pass |
| 7 | Foreign Origin is 403 | pass | **fail**: 200 |
| 8 | Unsupported MCP-Protocol-Version is 400 | pass | **fail**: 200 |
| 9 | No Mcp-Session-Id is 400 (SHOULD) | pass | warn (fails with `--alexa-mcp-strict`): 200 |
| 10 | Batch refused | pass | **fail**: both pings answered |
| 11 | Bad arguments are isError, no internals | pass | **fail**: `-32602` "HearthMCP._tool_record() got an unexpected keyword argument ..." |

Full output: [docs/hearth-run.txt](docs/hearth-run.txt) (current code) and
[docs/hearth-before-run.txt](docs/hearth-before-run.txt) (pre-fix code, plus a
strict run of check 9). The old code passed every Alexa+ check and failed the
transport rules. Those are the failures this tool exists to catch before a
reviewer does.

## Contributing

Issues and pull requests are welcome. Changes to a check need a test in
`tests/test_checks.py` that runs the bundled checks against the stdlib fake
server in `tests/fakeserver.py`:

- the conformant server must still pass every check, and
- a new flaw in `FLAWS` must fail the check it is about **and no other one**.

```sh
python -m venv .venv && .venv/bin/pip install -e .
.venv/bin/pytest -q
```

If you are adding a requirement, quote the sentence it comes from in the
check's docstring and in its failure message. If the Alexa+ docs change,
please open an issue with the page and the new wording.

## License

MIT, copyright 2026 Subomi Olagoke. See [LICENSE](LICENSE).

"""A deliberately small Streamable HTTP MCP client.

It exists so each check can send exactly the request it is about: with or
without a token, a session id, a protocol version header, an Origin. An SDK
client would fill those in for you, which is the opposite of what a
conformance check needs.

The bearer token is held here and nowhere else. Nothing in this module puts it
in a message, a repr or an exception.
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Any

import httpx

PROTOCOL_VERSION = "2025-11-25"
ACCEPT = "application/json, text/event-stream"
CLIENT_INFO = {"name": "alexa-plus-mcp-checks", "version": "0.1.0"}


class McpError(AssertionError):
    """The server did not answer a request the way a working MCP server must.

    Raised for requests that are only a means to an end in a check, such as
    tools/list before looking at a tool. A check that is about the answer
    inspects the raw response instead.
    """


def redact(text: str, secret: str | None) -> str:
    if secret:
        for form in {secret, urllib.parse.quote(secret, safe=""),
                     urllib.parse.quote_plus(secret)}:
            text = text.replace(form, "<token>")
    return text


def excerpt(resp: httpx.Response, secret: str | None = None, limit: int = 300) -> str:
    """Status and the start of the body, for failure messages."""
    body = resp.text.strip().replace("\n", " ")
    if len(body) > limit:
        body = body[:limit] + "..."
    return redact(f"HTTP {resp.status_code} {body}".strip(), secret)


def messages(resp: httpx.Response) -> list[Any]:
    """Every JSON value in a response body, whether it came back as plain JSON
    or as a text/event-stream. A Streamable HTTP server may answer a POST
    either way, and the client has to accept both."""
    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "text/event-stream":
        out, data = [], []
        for line in resp.text.splitlines() + [""]:
            if line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
            elif line == "" and data:
                try:
                    out.append(json.loads("\n".join(data)))
                except json.JSONDecodeError:
                    pass
                data = []
        return out
    if not resp.content:
        return []
    try:
        return [resp.json()]
    except (json.JSONDecodeError, UnicodeDecodeError):
        return []


def rpc_message(resp: httpx.Response, rpc_id: Any = None) -> Any:
    """The JSON-RPC response in a body: the one answering `rpc_id` if given,
    else the last one. A bare JSON value (an array, say) is returned as is."""
    found = messages(resp)
    if rpc_id is not None:
        for m in found:
            if isinstance(m, dict) and m.get("id") == rpc_id and (
                    "result" in m or "error" in m):
                return m
    return found[-1] if found else None


def origin_of(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return f"{p.scheme}://{p.netloc}"


class McpClient:
    """One kept-alive connection to one MCP endpoint."""

    def __init__(self, url: str, token: str | None = None, timeout: float = 10.0):
        self.url = url
        self._token = token
        self.http = httpx.Client(timeout=timeout, follow_redirects=False)
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._next_id = 0

    def __repr__(self) -> str:
        return (f"<McpClient {self.url} token={'set' if self._token else 'none'} "
                f"session={'yes' if self.session_id else 'no'}>")

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def redact(self, text: str) -> str:
        return redact(text, self._token)

    def excerpt(self, resp: httpx.Response) -> str:
        return excerpt(resp, self._token)

    def close(self) -> None:
        self.http.close()

    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def headers(self, *, auth: bool = True, session: bool = True,
                version: bool = True, extra: dict | None = None) -> dict:
        h = {"Accept": ACCEPT, "Content-Type": "application/json"}
        if auth and self._token:
            h["Authorization"] = f"Bearer {self._token}"
        if session and self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if version and self.protocol_version:
            h["MCP-Protocol-Version"] = self.protocol_version
        h.update(extra or {})
        return h

    def post(self, body: Any, *, auth: bool = True, session: bool = True,
             version: bool = True, headers: dict | None = None,
             token_in_query: bool = False) -> httpx.Response:
        """POST one body to the endpoint. `token_in_query` puts the token in
        `?access_token=`, which is only ever done to check it is refused."""
        params = {"access_token": self._token} if token_in_query else None
        try:
            return self.http.post(self.url, content=json.dumps(body), params=params,
                                  headers=self.headers(auth=auth, session=session,
                                                       version=version, extra=headers))
        except httpx.HTTPError as e:
            # httpx puts the request URL in some messages, and that URL can
            # carry the token when token_in_query is set.
            raise McpError(self.redact(f"request to the MCP endpoint failed: "
                                       f"{type(e).__name__}: {e}")) from None

    def request_body(self, method: str, params: dict | None = None,
                     rpc_id: Any = None) -> dict:
        body = {"jsonrpc": "2.0", "id": self.new_id() if rpc_id is None else rpc_id,
                "method": method}
        if params is not None:
            body["params"] = params
        return body

    def initialize(self, version: str = PROTOCOL_VERSION) -> httpx.Response:
        """initialize, then notifications/initialized, keeping the session id
        and the negotiated version for every later request."""
        self.session_id = None
        self.protocol_version = None
        body = self.request_body("initialize", {
            "protocolVersion": version, "capabilities": {}, "clientInfo": CLIENT_INFO})
        resp = self.post(body)
        msg = rpc_message(resp, body["id"])
        if resp.status_code == 200 and isinstance(msg, dict) and "result" in msg:
            self.session_id = resp.headers.get("mcp-session-id") or None
            self.protocol_version = msg["result"].get("protocolVersion") or version
            self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return resp

    def call(self, method: str, params: dict | None = None) -> dict:
        """A request whose answer is needed. Returns the `result` object, and
        raises McpError for anything but a 200 carrying a result."""
        body = self.request_body(method, params)
        resp = self.post(body)
        msg = rpc_message(resp, body["id"])
        if resp.status_code != 200 or not isinstance(msg, dict):
            raise McpError(f"{method} did not succeed: {self.excerpt(resp)}")
        if "error" in msg:
            raise McpError(self.redact(f"{method} returned a JSON-RPC error: "
                                       f"{json.dumps(msg['error'])[:300]}"))
        result = msg.get("result")
        if not isinstance(result, dict):
            raise McpError(f"{method} returned no result object: {self.excerpt(resp)}")
        return result

    def call_raw(self, method: str, params: dict | None = None) -> tuple[httpx.Response, Any]:
        """A request whose whole answer is under test: the HTTP response and
        the JSON-RPC message in it, unjudged."""
        body = self.request_body(method, params)
        resp = self.post(body)
        return resp, rpc_message(resp, body["id"])

    def list_all(self, method: str, key: str, limit: int = 50) -> list:
        """Follow nextCursor to the end of a paginated list."""
        out, cursor = [], None
        for _ in range(limit):
            result = self.call(method, {"cursor": cursor} if cursor else {})
            items = result.get(key)
            if not isinstance(items, list):
                raise McpError(f"{method} result has no '{key}' list")
            out.extend(items)
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return out

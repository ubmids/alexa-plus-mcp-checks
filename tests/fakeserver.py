"""A minimal MCP server on the standard library, for testing the checks.

With no flaws it satisfies every check. Each flaw breaks exactly one
requirement, which is how the tests prove each check can fail, and fail only
for its own reason.

Run it on its own (the CI workflow does, to exercise the GitHub Action):

    python tests/fakeserver.py --port 8765 [--flaw NAME ...]
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.parse
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROTOCOL = "2025-11-25"
SUPPORTED = {"2025-11-25", "2025-06-18", "2025-03-26"}
CARD = "ui://fake/card-1a2b3c4d.html"
APP_MIME = "text/html;profile=mcp-app"

# Each one violates a single requirement.
FLAWS = {
    "unauth_403": "answers an unauthenticated request 403, not 401",
    "www_authenticate": "adds WWW-Authenticate to the 401",
    "no_prm": "serves no protected resource metadata",
    "prm_wrong_resource": "PRM names a different resource",
    "no_as_metadata": "serves no authorization server metadata",
    "no_s256": "omits code_challenge_methods_supported",
    "query_token_ok": "accepts ?access_token=",
    "old_protocol": "negotiates 2025-06-18",
    "no_session_id": "issues no Mcp-Session-Id (and so requires none)",
    "ui_wrong_mime": "serves the view as text/html",
    "ui_not_listed": "leaves the view out of resources/list",
    "ui_result_no_meta": "tool results with structuredContent omit _meta.ui",
    "slow": "takes 600 ms per tools/list",
    "origin_ok": "ignores Origin",
    "any_version_ok": "ignores MCP-Protocol-Version",
    "sessionless_ok": "serves requests without Mcp-Session-Id after initialize",
    "batch_ok": "answers JSON-RPC batches",
    "args_protocol_error": "bad arguments are a -32602 protocol error",
    "args_leak": "bad arguments are isError with a traceback in the text",
    "sse": "answers every POST as text/event-stream (not a flaw: still conformant)",
}

TOOLS = [
    {"name": "echo", "title": "Echo", "description": "Says the text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "show_card", "title": "Show a card", "description": "Draws a card.",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer"}}},
     "_meta": {"ui": {"resourceUri": CARD}}},
]


def validate(tool, args):
    schema = tool["inputSchema"]
    props = schema.get("properties", {})
    if not isinstance(args, dict):
        return "arguments must be an object"
    missing = [k for k in schema.get("required", []) if k not in args]
    if missing:
        return "missing " + ", ".join(missing)
    unknown = sorted(k for k in args if k not in props)
    if unknown:
        return "unexpected " + ", ".join(unknown)
    return None


class FakeMcp(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0, flaws=(), token="good-token-0123456789abcdef"):
        unknown = set(flaws) - set(FLAWS)
        if unknown:
            raise ValueError(f"unknown flaws {sorted(unknown)}")
        self.flaws = set(flaws)
        self.token = token
        self.sessions: set[str] = set()
        super().__init__(("127.0.0.1", port), Handler)
        self.base = f"http://127.0.0.1:{self.server_address[1]}"
        self.url = self.base + "/mcp"

    def start(self):
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.shutdown()
        self.server_close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    disable_nagle_algorithm = True
    server: FakeMcp

    def log_message(self, *a):
        pass

    # --- plumbing ---------------------------------------------------------
    def send(self, status, body=None, headers=None):
        flaws = self.server.flaws
        if body is None:
            raw, ctype = b"", None
        elif "sse" in flaws and self.command == "POST" and status == 200:
            raw = f"event: message\ndata: {json.dumps(body)}\n\n".encode()
            ctype = "text/event-stream"
        else:
            raw, ctype = json.dumps(body).encode(), "application/json"
        self.send_response(status)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def rpc_error(self, status, rpc_id, code, message, headers=None):
        self.send(status, {"jsonrpc": "2.0", "id": rpc_id,
                           "error": {"code": code, "message": message}}, headers)

    # --- metadata ---------------------------------------------------------
    def do_GET(self):
        f, base = self.server.flaws, self.server.base
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/.well-known/oauth-protected-resource",
                    "/.well-known/oauth-protected-resource/mcp") and "no_prm" not in f:
            resource = base + ("/other" if "prm_wrong_resource" in f else "/mcp")
            return self.send(200, {"resource": resource, "authorization_servers": [base],
                                   "bearer_methods_supported": ["header"]})
        if path == "/.well-known/oauth-authorization-server" and "no_as_metadata" not in f:
            meta = {"issuer": base, "authorization_endpoint": base + "/authorize",
                    "token_endpoint": base + "/token", "response_types_supported": ["code"]}
            if "no_s256" not in f:
                meta["code_challenge_methods_supported"] = ["S256"]
            return self.send(200, meta)
        self.send(404, {"error": "not found"})

    # --- the MCP endpoint -------------------------------------------------
    def do_POST(self):
        srv, f = self.server, self.server.flaws
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        url = urllib.parse.urlsplit(self.path)
        if url.path != "/mcp":
            return self.send(404, {"error": "not found"})

        origin = self.headers.get("Origin")
        if origin and "origin_ok" not in f:
            host = urllib.parse.urlsplit(origin).hostname or ""
            if origin.rstrip("/") != srv.base and host not in ("localhost", "127.0.0.1"):
                return self.rpc_error(403, None, -32600, "Forbidden: Origin not allowed")

        pv = self.headers.get("MCP-Protocol-Version")
        if pv is not None and pv not in SUPPORTED and "any_version_ok" not in f:
            return self.rpc_error(400, None, -32600, f"Unsupported MCP-Protocol-Version: {pv}")

        query = urllib.parse.parse_qs(url.query)
        if "access_token" in query and "query_token_ok" not in f:
            return self.send(400, {"error": "invalid_request"})

        auth = self.headers.get("Authorization") or ""
        authed = auth == f"Bearer {srv.token}"
        if "query_token_ok" in f and query.get("access_token") == [srv.token]:
            authed = True
        if not authed:
            headers = ({"WWW-Authenticate": 'Bearer resource_metadata="x"'}
                       if "www_authenticate" in f else None)
            status = 403 if "unauth_403" in f else 401
            return self.send(status, {"error": "unauthorized"}, headers)

        try:
            msg = json.loads(raw or b"")
        except json.JSONDecodeError:
            return self.rpc_error(400, None, -32700, "Parse error")
        headers = {"MCP-Protocol-Version": PROTOCOL}

        if isinstance(msg, list):
            if "batch_ok" in f:
                return self.send(200, [self.dispatch(m) for m in msg], headers)
            return self.rpc_error(400, None, -32600, "Invalid Request: no batches", headers)
        if not isinstance(msg, dict):
            return self.rpc_error(400, None, -32600, "Invalid Request", headers)

        sid = self.headers.get("Mcp-Session-Id")
        if msg.get("method") == "initialize":
            if "no_session_id" not in f:
                sid = uuid.uuid4().hex
                srv.sessions.add(sid)
                headers["Mcp-Session-Id"] = sid
        elif "no_session_id" not in f:
            if not sid and "sessionless_ok" not in f:
                return self.rpc_error(400, msg.get("id"), -32600, "Mcp-Session-Id required",
                                      headers)
            if sid and sid not in srv.sessions:
                return self.send(404, {"error": "no such session"}, headers)

        out = self.dispatch(msg)
        if out is None:
            return self.send(202, None, headers)
        self.send(200, out, headers)

    def dispatch(self, msg):
        f = self.server.flaws
        method, rpc_id = msg.get("method"), msg.get("id")
        params = msg.get("params") or {}

        def ok(result):
            return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

        if "id" not in msg:
            return None
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in SUPPORTED else PROTOCOL
            if "old_protocol" in f:
                version = "2025-06-18"
            return ok({"protocolVersion": version,
                       "capabilities": {"tools": {}, "resources": {}},
                       "serverInfo": {"name": "fake", "version": "0"}})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            if "slow" in f:
                time.sleep(0.6)
            return ok({"tools": TOOLS})
        if method == "resources/list":
            listed = [] if "ui_not_listed" in f else [
                {"uri": CARD, "name": "card", "mimeType": APP_MIME}]
            return ok({"resources": listed})
        if method == "resources/read":
            if params.get("uri") != CARD:
                return {"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32002, "message": "Resource not found"}}
            mime = "text/html" if "ui_wrong_mime" in f else APP_MIME
            return ok({"contents": [{"uri": CARD, "mimeType": mime,
                                     "text": "<!doctype html><p>card</p>"}]})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            tool = next((t for t in TOOLS if t["name"] == name), None)
            if tool is None:
                return {"jsonrpc": "2.0", "id": rpc_id,
                        "error": {"code": -32602, "message": f"Unknown tool: {name}"}}
            problem = validate(tool, args)
            if problem:
                if "args_protocol_error" in f:
                    return {"jsonrpc": "2.0", "id": rpc_id,
                            "error": {"code": -32602, "message": f"Invalid params: {problem}"}}
                text = f"Invalid arguments for {name}: {problem}"
                if "args_leak" in f:
                    text = ("Traceback (most recent call last):\n  File \"server.py\", "
                            f"line 12, in call\nTypeError: {name}() got an unexpected "
                            "keyword argument")
                return ok({"content": [{"type": "text", "text": text}], "isError": True})
            if name == "echo":
                return ok({"content": [{"type": "text", "text": args["text"]}],
                           "isError": False})
            result = {"content": [{"type": "text", "text": "A card."}], "isError": False,
                      "structuredContent": {"title": "A card", "limit": args.get("limit")}}
            if "ui_result_no_meta" not in f:
                result["_meta"] = {"ui": {"resourceUri": CARD}}
            return ok(result)
        return {"jsonrpc": "2.0", "id": rpc_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", default="good-token-0123456789abcdef")
    p.add_argument("--flaw", action="append", default=[], choices=sorted(FLAWS))
    ns = p.parse_args()
    srv = FakeMcp(ns.port, ns.flaw, ns.token)
    print(f"fake MCP server on {srv.url}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

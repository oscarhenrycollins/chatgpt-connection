"""Wizard MCP front: serves this app's MCP tools at /mcp and passes every other request,
byte for byte, to the app behind it (HTTP, streaming and WebSockets alike).
Stdlib only, so it runs on a stock python image with no build step.
Generated from the app repo's mcp/ folder; edit there, then run mcp/inline.py."""
import asyncio
import contextvars
import base64
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request
import uuid

APP = os.environ.get("WIZARD_APP", "app")
VERSION = os.environ.get("APP_VERSION", "0")
PORT = int(os.environ.get("FRONT_PORT", "8080"))
UP_HOST = os.environ.get("UPSTREAM_HOST", "")
UP_PORT = int(os.environ.get("UPSTREAM_PORT", str(PORT)))
MCP_PATH = "/mcp"
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")  # when set, /mcp needs Authorization: Bearer <token>
MAX_HEAD = 65536
MAX_BODY = 1 << 20
TOOLS = {}
REQUEST = contextvars.ContextVar("request", default={})  # headers of the current /mcp call

# Browser boundary (default-off, WIZARD_BROWSER_BOUNDARY=1): this listener is reachable straight from the private
# network (Tailscale Serve publishes the app's path and port to it directly, not through the shell), so network owner
# identity is not authority here. The accounts routes (WIZARD_GUARDED_PATHS, prefixes) then answer only the Wizard
# Agent Service adapter: a box-local request (no browser or proxy headers) that carries this installation's app
# credential (X-Wizard-App-Credential, minted by the App Manager, never given to a browser). /api/connection and
# /api/infer keep their own grants (Agent Service usage reader, connection router) and are not affected.
# Preflights are refused on guarded paths, and no CORS header is ever sent. wizard-os spec/browser-trust-shell-intents.
BOUNDARY = os.environ.get("WIZARD_BROWSER_BOUNDARY", "0") == "1"
APP_CREDENTIAL = os.environ.get("WIZARD_APP_CREDENTIAL", "").strip()
GUARDED = tuple(p.strip() for p in os.environ.get("WIZARD_GUARDED_PATHS", "").split(",") if p.strip())
# The optional page password (a box with one set): the gated adapter call is let through to the engine with it, since
# the adapter never holds the owner's password. Never forwarded from a request.
PAGE_PASSWORD = os.environ.get("WIZARD_PAGE_PASSWORD", "")
CREDENTIAL_HEADER = "x-wizard-app-credential"
BROWSER_HEADERS = ("origin", "cookie", "referer", "sec-fetch-site", "sec-fetch-dest", "sec-fetch-user")
NETWORK_HEADERS = ("tailscale-user-login", "tailscale-user-name", "x-forwarded-for", "x-forwarded-host", "forwarded",
                   "x-real-ip", "via")


def boundary_config_error():
    """Why the boundary cannot start, or "" (fail closed: never run unguarded with the flag on)."""
    if not BOUNDARY:
        return ""
    if len(APP_CREDENTIAL) != 64 or any(c not in "0123456789abcdef" for c in APP_CREDENTIAL):
        return "WIZARD_BROWSER_BOUNDARY=1 needs WIZARD_APP_CREDENTIAL (64 lowercase hex, from the App Manager)"
    if not GUARDED or not all(p.startswith("/") and p.endswith("/") for p in GUARDED):
        return "WIZARD_BROWSER_BOUNDARY=1 needs WIZARD_GUARDED_PATHS (comma-separated /prefix/ paths)"
    return ""


def guarded(path):
    return any(path == p.rstrip("/") or path.startswith(p) for p in GUARDED)


def listener_gate(method, path, raw_headers):
    """(status, error) when the boundary refuses this request, else None. raw_headers: [(lower name, value)]."""
    if not BOUNDARY or not guarded(path):
        return None
    if method == "OPTIONS":
        return 403, "preflight_refused"
    names = [k for k, _ in raw_headers]
    if any(k in BROWSER_HEADERS for k in names):
        return 403, "browser_request_refused"       # frames and pages, even with a credential they should never hold
    if any(k in NETWORK_HEADERS or k.startswith("cf-") for k in names):
        return 403, "network_identity_refused"      # an owner login from the network is not app authority
    supplied = [v for k, v in raw_headers if k == CREDENTIAL_HEADER]
    import hmac
    if len(supplied) != 1 or not hmac.compare_digest(supplied[0].strip().encode("latin-1"), APP_CREDENTIAL.encode()):
        return 401, "adapter_credential_required"
    return None


def gated_head_lines(lines):
    """Request head lines for an admitted adapter call: the credential and any Authorization are dropped; the page
    password (if this box has one) is added so the engine's own page check passes."""
    out = [l for l in lines if not l.lower().startswith((CREDENTIAL_HEADER + ":", "authorization:"))]
    if PAGE_PASSWORD:
        out.insert(1, "Authorization: Basic " + base64.b64encode(("wizard:" + PAGE_PASSWORD).encode()).decode())
    return out


def tool(name, description, props=None, required=()):
    def deco(fn):
        schema = {"type": "object", "properties": props or {}, "required": list(required)}
        TOOLS[name] = ({"name": name, "description": description, "inputSchema": schema}, fn)
        return fn
    return deco


def up_addr():
    try:
        return socket.gethostbyname(UP_HOST)
    except OSError:
        return UP_HOST


def http(method, path, body=None, headers=None, timeout=60):
    url = "http://%s:%d%s" % (up_addr(), UP_PORT, path)
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read()


def err(i, code, message):
    return {"jsonrpc": "2.0", "id": i, "error": {"code": code, "message": message}}


def rpc(msg):
    m, i, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if m == "initialize":
        r = {"protocolVersion": p.get("protocolVersion", "2025-06-18"), "capabilities": {"tools": {}},
             "serverInfo": {"name": APP, "version": VERSION}}
    elif m == "tools/list":
        r = {"tools": [t[0] for t in TOOLS.values()]}
    elif m == "tools/call":
        name, args = p.get("name"), p.get("arguments") or {}
        if name not in TOOLS:
            return err(i, -32602, "unknown tool %s" % name)
        try:
            out = TOOLS[name][1](**args)
            if isinstance(out, dict) and "_content" in out:
                r = {"content": out["_content"]}
            else:
                text = out if isinstance(out, str) else json.dumps(out, indent=2)
                r = {"content": [{"type": "text", "text": text[:200000]}]}
        except Exception as e:  # tool errors go back to the agent, not the transport
            r = {"content": [{"type": "text", "text": "%s: %s" % (type(e).__name__, e)}], "isError": True}
    elif m == "ping":
        r = {}
    elif i is None:
        return None  # notification
    else:
        return err(i, -32601, "unknown method %s" % m)
    return {"jsonrpc": "2.0", "id": i, "result": r}


REASONS = {200: "OK", 202: "Accepted", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
           401: "Unauthorized", 413: "Payload Too Large", 502: "Bad Gateway"}


async def respond(writer, code, obj=None, extra=""):
    body = b"" if obj is None else json.dumps(obj).encode()
    head = "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n%s\r\n" % (
        code, REASONS.get(code, "OK"), len(body), extra)
    writer.write(head.encode() + body)
    try:
        await writer.drain()
    finally:
        writer.close()


async def mcp(reader, writer, method, rest):
    headers = {}
    for line in rest.decode("latin-1").split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    if MCP_TOKEN:
        import hmac
        if not hmac.compare_digest(headers.get("authorization", "").encode(), ("Bearer " + MCP_TOKEN).encode()):
            return await respond(writer, 401, {"error": "this MCP needs Authorization: Bearer <token>"}, 'WWW-Authenticate: Bearer\r\n')
    if method != "POST":
        return await respond(writer, 405, {"error": "use POST %s" % MCP_PATH}, "Allow: POST\r\n")
    n = int(headers.get("content-length") or 0)
    if n > MAX_BODY:
        return await respond(writer, 413, {"error": "request too large"})
    body = await reader.readexactly(n) if n else b""
    try:
        msg = json.loads(body or b"{}")
    except ValueError:
        return await respond(writer, 400, err(None, -32700, "parse error"))
    REQUEST.set(headers)
    batch = msg if isinstance(msg, list) else [msg]
    results = await asyncio.gather(*(asyncio.to_thread(rpc, x) for x in batch if isinstance(x, dict)))
    out = [x for x in results if x]
    if not out:
        return await respond(writer, 202)
    await respond(writer, 200, out if isinstance(msg, list) else out[0])


async def pipe(r, w):
    try:
        while True:
            data = await r.read(65536)
            if not data:
                break
            w.write(data)
            await w.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def handle(reader, writer):
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        writer.close()
        return
    line, _, rest = head.partition(b"\r\n")
    parts = line.decode("latin-1").split(" ")
    path = parts[1].split("?", 1)[0] if len(parts) >= 3 else ""
    if path == MCP_PATH:
        return await mcp(reader, writer, parts[0], rest)
    admitted = False
    if BOUNDARY:
        raw = [(k.strip().lower(), v.strip()) for k, _, v in
               (l.partition(":") for l in rest.decode("latin-1").split("\r\n") if ":" in l)]
        refused = listener_gate(parts[0], path, raw)
        if refused:
            return await respond(writer, refused[0], {"error": refused[1]}, "Cache-Control: no-store\r\n")
        admitted = guarded(path)
    if not UP_HOST:
        return await respond(writer, 404, {"error": "not found"})
    try:
        ur, uw = await asyncio.open_connection(UP_HOST, UP_PORT)
    except OSError:
        return await respond(writer, 502, {"error": "%s is starting or not reachable" % APP})
    # One request per proxied connection (unless it is an upgrade such as a WebSocket), so a
    # later /mcp request can never ride an upstream keep-alive connection.
    lines = head.decode("latin-1").split("\r\n")
    if admitted:
        lines = gated_head_lines(lines)
        head = "\r\n".join(lines).encode("latin-1")
    if not any(l.lower().startswith("upgrade:") for l in lines):
        lines = [l for l in lines if l and not l.lower().startswith(("connection:", "keep-alive:"))]
        head = ("\r\n".join(lines) + "\r\nConnection: close\r\n\r\n").encode("latin-1")
    uw.write(head)
    await uw.drain()
    await asyncio.gather(pipe(reader, uw), pipe(ur, writer))


async def main():
    problem = boundary_config_error()
    if problem:
        raise SystemExit(problem)
    srv = await asyncio.start_server(handle, "0.0.0.0", PORT, limit=MAX_HEAD)
    async with srv:
        await srv.serve_forever()

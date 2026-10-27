"""ChatGPT app owns OAuth; Hermes receives only a revocable box-local bearer."""
from __future__ import annotations

import base64
import hmac
import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import socketserver
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPCookieProcessor

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inference
import codex

VERSION = "0.3.1"
CONTRACT = "wizard.ai-connection/2"
APP = Path(__file__).resolve().parent
BIND = os.environ.get("WIZARD_CHATGPT_BIND", "0.0.0.0")
PORT = int(os.environ.get("WIZARD_CHATGPT_PORT", "8801"))
PASSWORD = os.environ.get("WIZARD_CHATGPT_PASSWORD", "")
HERMES_URL = (os.environ.get("WIZARD_CHATGPT_HERMES_URL") or "http://hermes-agent:9119").rstrip("/")
HERMES_USER = os.environ.get("WIZARD_CHATGPT_HERMES_USER") or "hermes"
HERMES_PASSWORD = os.environ.get("WIZARD_CHATGPT_HERMES_PASSWORD", "")
AGENT_BASE = os.environ.get("WIZARD_CHATGPT_AGENT_URL") or f"http://wizard-chatgpt:{PORT}/v1"
ENDPOINT_ID = "wizard-chatgpt"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/appearance.js": ("appearance.js", "text/javascript"), "/appearance.css": ("appearance.css", "text/css"),
          "/app.js": ("app.js", "text/javascript"), "/app.css": ("app.css", "text/css"), "/SKILL.md": ("SKILL.md", "text/markdown")}
ORIGIN_PATTERN = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
FRAME_ANCESTORS = " ".join(["'self'"] + [o for o in (x.strip().rstrip("/") for x in os.environ.get("WIZARD_TRUSTED_ORIGINS", "").split(",")) if ORIGIN_PATTERN.match(o)])
OWNER = None
GATEWAY_SLOTS = threading.BoundedSemaphore(8)
INIT_LOCK = threading.Lock()
REGISTRATION_LOCK = threading.RLock()


def owner():
    global OWNER
    with INIT_LOCK:
        if OWNER is None:
            from account_owner import Pool
            OWNER = Pool(os.environ.get("APP_DATA_DIR", str(APP / ".data")))
    return OWNER


class HermesError(Exception):
    code = "unreachable"


class Hermes:
    """Dashboard registration only. Never drives OAuth or model requests."""
    lock = threading.Lock()
    opener = None

    @classmethod
    def configured(cls):
        return bool(HERMES_PASSWORD)

    @classmethod
    def _login(cls):
        jar = http.cookiejar.CookieJar()
        op = build_opener(ProxyHandler({}), codex.NoRedirect(), HTTPCookieProcessor(jar))
        req = Request(HERMES_URL + "/auth/password-login", data=json.dumps({"provider": "basic", "username": HERMES_USER, "password": HERMES_PASSWORD}).encode(), headers={"Content-Type": "application/json"})
        with op.open(req, timeout=20) as r:
            r.read()
        cls.opener = op

    @classmethod
    def call(cls, method, path, body=None, timeout=40):
        if not cls.configured():
            raise HermesError("Hermes dashboard is not configured.")
        for attempt in (0, 1):
            try:
                with cls.lock:
                    if cls.opener is None:
                        cls._login()
                    op = cls.opener
                    assert op is not None
                req = Request(HERMES_URL + path, data=None if body is None else json.dumps(body).encode(), method=method, headers={"Content-Type": "application/json"})
                with op.open(req, timeout=timeout) as r:
                    return json.loads(r.read(4 * 1024 * 1024) or b"{}")
            except HTTPError as e:
                status = e.code
                e.close()
                if status == 401 and not attempt:
                    with cls.lock:
                        cls.opener = None
                    continue
                raise HermesError("Hermes dashboard registration was rejected.") from None
            except (OSError, ValueError):
                raise HermesError("Hermes dashboard is unavailable.") from None
        raise HermesError("Hermes dashboard login was rejected.")


def pool_rows(payload):
    return next((r.get("entries", []) for r in payload.get("providers", []) if r.get("provider") == "openai-codex"), [])


def register(models):
    with REGISTRATION_LOCK:
        if owner().stored().get("status") != "connected":
            return "not_registered"
        return _register(models)


def _register(models):
    """Register native Codex routing without changing any model selection.

    Stock dashboard custom-endpoints are *always* custom runtimes, so also provision the
    native provider's API-key pool entry + box URL override. No OAuth/refresh token in Hermes.
    Refuse coexistence with other Codex credentials rather than redirect them to our box.
    """
    if not models or not Hermes.configured():
        return "unavailable"
    key = owner().gateway_token()
    try:
        rows = pool_rows(Hermes.call("GET", "/api/credentials/pool"))
        if any(r.get("label") != ENDPOINT_ID for r in rows):
            return "conflict: remove existing Hermes Codex credentials before app-owned routing"
        if not rows:
            oauth = Hermes.call("GET", "/api/providers/oauth").get("providers", [])
            status = next((p.get("status", {}) for p in oauth if p.get("id") == "openai-codex"), None)
            if status is None or status.get("error"):
                return "unavailable"
            if status.get("logged_in") or status.get("token_preview"):
                return "conflict: remove existing Hermes Codex credentials before app-owned routing"
        # A named custom endpoint is discoverable and provides an explicit fallback.
        ids = [m["id"] for m in models]
        Hermes.call("POST", "/api/providers/custom-endpoints", {"id": ENDPOINT_ID, "name": "ChatGPT app", "base_url": AGENT_BASE,
                    "model": ids[0], "models": ids, "api_key": key, "api_mode": "codex_responses", "discover_models": False, "make_default": False})
        listed = Hermes.call("GET", "/api/providers/custom-endpoints").get("endpoints", [])
        if not any(r.get("id") == ENDPOINT_ID and r.get("base_url", "").rstrip("/") == AGENT_BASE.rstrip("/") for r in listed):
            return "unavailable"
        Hermes.call("PUT", "/api/env", {"key": "HERMES_CODEX_BASE_URL", "value": AGENT_BASE})
        import hashlib
        fingerprint = hashlib.sha256(key.encode()).hexdigest()
        try:
            previous = json.loads((owner().data / "registration.json").read_text())
        except (OSError, ValueError):
            previous = {}
        if len(rows) != 1 or previous.get("key_sha256") != fingerprint or previous.get("base_url") != AGENT_BASE:
            for row in reversed(rows):
                Hermes.call("DELETE", f"/api/credentials/pool/openai-codex/{row['index']}")
            Hermes.call("POST", "/api/credentials/pool", {"provider": "openai-codex", "api_key": key, "label": ENDPOINT_ID})
        check = pool_rows(Hermes.call("GET", "/api/credentials/pool"))
        return "registered" if any(r.get("label") == ENDPOINT_ID and not r.get("has_refresh") for r in check) else "unavailable"
    except HermesError:
        return "unavailable"


def registration():
    try:
        return json.loads((owner().data / "registration.json").read_text()).get("status", "not_registered")
    except (OSError, ValueError):
        return "not_registered"


def reconcile():
    import hashlib
    with REGISTRATION_LOCK:
        try:
            result = register(owner().models())
            try:
                with owner().lock():
                    owner().atomic("registration.json", {"status": result, **({"base_url": AGENT_BASE, "key_sha256": hashlib.sha256(owner().gateway_token().encode()).hexdigest()} if result == "registered" else {})})
            except OSError:
                pass  # Login is independent of advisory dashboard-registration status.
            return result
        except codex.Fault:
            return "unavailable"


def unregister():
    with REGISTRATION_LOCK:
        return _unregister()


def _unregister():
    failed = False
    # Only remove the app-labelled pool row. Never log out a different Codex login.
    try:
        rows = pool_rows(Hermes.call("GET", "/api/credentials/pool"))
        for i in reversed(range(len(rows))):
            if rows[i].get("label") == ENDPOINT_ID:
                Hermes.call("DELETE", f"/api/credentials/pool/openai-codex/{rows[i]['index']}")
        endpoints = Hermes.call("GET", "/api/providers/custom-endpoints").get("endpoints", [])
        if any(r.get("id") == ENDPOINT_ID for r in endpoints):
            Hermes.call("DELETE", "/api/providers/custom-endpoints/" + ENDPOINT_ID)
        # Keep URL override fail-closed: removing it could send a residual local bearer to chatgpt.com.
        if any(r.get("label") == ENDPOINT_ID for r in pool_rows(Hermes.call("GET", "/api/credentials/pool"))):
            failed = True
        if any(r.get("id") == ENDPOINT_ID for r in Hermes.call("GET", "/api/providers/custom-endpoints").get("endpoints", [])):
            failed = True
    except HermesError:
        failed = Hermes.configured()
    with owner().lock():
        owner().atomic("registration.json", {"status": "unregister_failed" if failed else "not_registered"})


def connection():
    problem = None
    try:
        s = owner().stored()
    except codex.Fault as e:
        s, problem = {"status": "error"}, {"code": e.code, "message": str(e)}
    status = "expired" if s.get("status") == "refreshing" else s.get("status", "not_connected")
    out = {"contract": CONTRACT, "provider": "chatgpt", "name": "ChatGPT", "status": status,
           "plan": None, "account": None, "usage": None, "error": None, "models": [], "chatIntegration": registration()}
    if problem:
        out["error"] = problem
        return out
    if status == "connected":
        try:
            out["models"] = owner().models()
            out["plan"], out["usage"] = owner().usage()
        except codex.Fault as e:
            current = owner().stored().get("status", status)
            out["status"] = ("error" if owner().unsaved is not None else "expired") if current == "refreshing" else current
            out["error"] = {"code": e.code, "message": str(e)}
    else:
        out["error"] = {"code": "expired" if status == "expired" else "signed_out", "message": "Open ChatGPT and press Connect."}
    return out


def account_row():
    try:
        s = owner().stored()
    except codex.Fault as e:
        return {"id": "chatgpt-subscription", "name": "ChatGPT subscription", "status": "error", "problem": str(e)}
    if owner().unsaved is not None:
        s = {"status": "connected"}
    return {"id": "chatgpt-subscription", "name": "ChatGPT subscription", "status": "expired" if s.get("status") == "refreshing" else s.get("status", "not_connected"),
            "detail": "Login not safely saved — repair app-data storage and press Test before restarting." if owner().unsaved is not None else "This app stores and renews your ChatGPT login. Hermes uses a box-only connection.",
            "account": "", "signing_in": bool(owner().login), "integration": registration()}


# Only these fixed routes can reach the backend. Never an arbitrary forwarding proxy.
GET_GATEWAY = {"/v1/models": "/codex/models?client_version=99.0.0",
               "/v1/api/codex/usage": "/wham/usage",
               "/v1/api/codex/rate-limit-reset-credits": "/wham/rate-limit-reset-credits"}
POST_GATEWAY = {"/v1/responses": "/codex/responses", "/v1/responses/compact": "/codex/responses/compact",
                "/v1/api/codex/rate-limit-reset-credits/consume": "/wham/rate-limit-reset-credits/consume"}


# Node's built-in fetch (undici) always adds `Sec-Fetch-Mode: cors`, so that one header alone is a
# box-local server caller (the Wizard Agent Service reading usage), not a browser. Browsers also send
# Sec-Fetch-Site/Dest, which stay refused along with every proxy and cookie header.
SERVER_FETCH_HEADERS = frozenset({"sec-fetch-mode"})


def browser_or_proxy(key):
    k = key.lower()
    return (k in {"origin", "referer", "cookie", "tailscale-user-login", "forwarded", "x-real-ip", "via"}
            or (k.startswith(("cf-", "x-forwarded-", "sec-fetch-")) and k not in SERVER_FETCH_HEADERS))


def box_only(headers):
    return not any(browser_or_proxy(k) for k in headers)


class Handler(BaseHTTPRequestHandler):
    server_version = "wizard-chatgpt"
    sys_version = ""

    def log_message(self, format, *args):
        pass  # no query strings, headers, request bodies or upstream exception text in logs

    def send(self, code, body, kind="application/json"):
        self.send_response(code)
        for k, v in {"Content-Type": kind, "Content-Length": str(len(body)), "Cache-Control": "no-store",
                     "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Connection": "close",
                     "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors " + FRAME_ANCESTORS + "; base-uri 'none'; form-action 'none'"}.items():
            self.send_header(k, v)
        self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, code, data):
        self.send(code, json.dumps(data).encode())

    def authorized(self, path):
        if path == "/api/connection" and self.headers.get("X-Wizard-Grant"):
            try:
                if inference.admit(self.headers):
                    return True
            except inference.Fault as e:
                self.json(e.status, e.payload())
                return False
        if not PASSWORD or path == "/health":
            return True
        try:
            h = self.headers.get("Authorization", "")
            if h.startswith("Basic ") and hmac.compare_digest(base64.b64decode(h[6:]).decode().partition(":")[2].encode(), PASSWORD.encode()):
                return True
        except (ValueError, UnicodeError):
            pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ChatGPT", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True
        return False

    def gateway(self, path):
        if not box_only(self.headers):
            return self.json(403, {"error": "Box-only endpoint."})
        given = self.headers.get("Authorization", "")
        if len(self.headers.get_all("Authorization", [])) != 1 or not hmac.compare_digest(given.encode(), ("Bearer " + owner().gateway_token()).encode()):
            return self.json(401, {"error": "Local gateway bearer required."})
        routes = GET_GATEWAY if self.command in ("GET", "HEAD") else POST_GATEWAY
        if path not in routes:
            return self.json(404, {"error": "Unsupported gateway route."})
        if not GATEWAY_SLOTS.acquire(blocking=False):
            return self.json(429, {"error": "Gateway capacity busy."})
        started = False
        try:
            self.connection.settimeout(120)
            body = inference.body(self, 16 * 1024 * 1024) if self.command == "POST" else None
            if path == "/v1/responses":
                assert body is not None
                body.update(store=False, stream=True, instructions=body.get("instructions") or "You are a helpful assistant.")
                for k in ("max_output_tokens", "max_tokens", "temperature", "top_p", "frequency_penalty", "presence_penalty", "truncation"):
                    body.pop(k, None)
            extra = {k: self.headers[k] for k in ("session_id", "x-client-request-id") if self.headers.get(k)}
            with owner().open(self.command if self.command != "HEAD" else "GET", routes[path], body, extra) as r:
                if path == "/v1/models":
                    data = json.loads(r.read(codex.MAX_JSON))
                    rows = [m for m in data.get("models", []) if isinstance(m, dict) and m.get("slug")]
                    # Both shapes: OpenAI `data` for generic clients, and the account-scoped Codex `models`
                    # rows that stock Hermes' native openai-codex reads. Without `models` Hermes falls back to
                    # its offline list, which drops account-gated models such as GPT-6 Astra.
                    return self.json(200, {"object": "list", "data": [{"id": m["slug"], "object": "model", "owned_by": "chatgpt"} for m in rows], "models": rows})
                if path != "/v1/responses":
                    raw = r.read(codex.MAX_JSON + 1)
                    if len(raw) > codex.MAX_JSON:
                        raise codex.Fault(message="ChatGPT response exceeded its limit.")
                    return self.send(r.status, raw, "application/json")
                self.send_response(r.status)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.send_header("X-Accel-Buffering", "no")
                # Quota headers only, never cookies, upstream identity or auth headers.
                for k, v in r.headers.items():
                    if k.lower().startswith(("x-codex-", "x-ratelimit-")) or k.lower() == "retry-after":
                        self.send_header(k, v)
                self.end_headers()
                self.close_connection = True
                started = True
                if self.command != "HEAD":
                    # SSE lines are flushed immediately, not buffered until completion.
                    deadline, total = time.monotonic() + 600, 0
                    while True:
                        line = r.readline(4 * 1024 * 1024 + 1)
                        if not line:
                            break
                        total += len(line)
                        if len(line) > 4 * 1024 * 1024 or total > 64 * 1024 * 1024 or time.monotonic() > deadline:
                            raise codex.Fault("limit", "ChatGPT stream exceeded its limit.", 429)
                        self.wfile.write(line)
                        self.wfile.flush()
        except (codex.Fault, inference.Fault) as e:
            if not started:
                self.json(e.status, {"error": {"code": e.code, "message": str(e)}})
            else:
                self.wfile.write(("event: error\ndata: " + json.dumps({"type": "error", "code": e.code, "message": str(e)}) + "\n\n").encode())
                self.wfile.flush()
        except (OSError, ValueError):
            if not started:
                self.json(502, {"error": "ChatGPT gateway unavailable."})
        finally:
            GATEWAY_SLOTS.release()

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/v1/"):
            return self.gateway(path)
        if not self.authorized(path):
            return
        if path == "/health":
            return self.json(200, {"status": "ok", "version": VERSION})
        if path == "/api/connection":
            if not box_only(self.headers):
                return self.json(403, {"error": "box-only endpoint"})
            return self.json(200, connection())
        if path == "/api/accounts":
            return self.json(200, {"accounts": [account_row()], "version": VERSION})
        if path == "/.well-known/mcp/server-cards.json":
            return self.json(200, {"name": "wizard-chatgpt", "version": VERSION, "title": "ChatGPT", "remotes": [],
                        "x-wizard": {"provides": ["ai-connection", "chatgpt-subscription"], "contract": CONTRACT,
                                     "ai_calls": True, "secrets_exposed": False}})
        if path in STATIC:
            name, kind = STATIC[path]
            return self.send(200, ((APP if name == "SKILL.md" else APP / "static") / name).read_bytes(), kind)
        self.json(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith("/v1/"):
            return self.gateway(path)
        if path == "/api/infer":
            return inference.handle_owned(self, owner())
        if not self.authorized(path):
            return
        origin = self.headers.get("Origin")
        if (origin and urlsplit(origin).netloc != self.headers.get("Host")) or self.headers.get("Sec-Fetch-Site") == "cross-site":
            return self.json(403, {"ok": False, "message": "Cross-site request refused."})
        # A same-origin JS-only header closes simple-form/opaque-Origin CSRF, including Basic auth.
        if self.headers.get("X-Wizard-Request") != "1":
            return self.json(403, {"ok": False, "message": "Use the ChatGPT page."})
        try:
            data = inference.body(self)
            if path == "/api/chatgpt/connect":
                return self.json(200, owner().connect())
            if path == "/api/chatgpt/poll":
                result = owner().poll(str(data.get("login_id", "")))
                if result.get("status") == "approved":
                    result["registration"] = reconcile()
                return self.json(200, result)
            if path == "/api/chatgpt/cancel":
                owner().cancel()
                return self.json(200, {"ok": True})
            if path == "/api/chatgpt/test":
                owner().access()
                models = owner().models()
                plan, usage = owner().usage()
                return self.json(200, {"ok": True, "message": "ChatGPT connection verified.", "plan": plan, "usage": usage, "models": models, "registration": reconcile()})
            if path == "/api/chatgpt/logout":
                with REGISTRATION_LOCK:
                    owner().disconnect()
                    unregister()
                return self.json(200, {"ok": True, "registration": registration(), "message": "Disconnected locally; Hermes cleanup needs retry." if registration() == "unregister_failed" else "Disconnected. In-flight replies may finish; new requests are disabled."})
            self.json(404, {"ok": False, "message": "not found"})
        except (codex.Fault, inference.Fault) as e:
            self.json(e.status, {"ok": False, "status": e.code, "message": str(e)})
        except (OSError, ValueError, TypeError):
            self.json(503, {"ok": False, "message": "ChatGPT connection unavailable."})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]


def main():
    os.umask(0o077)
    owner()
    server = Server((BIND, PORT), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    if owner().stored().get("status") == "connected":
        threading.Thread(target=reconcile, daemon=True).start()
    print(f"ChatGPT {VERSION} on {BIND}:{PORT}", flush=True)
    try:
        server.serve_forever()
    finally:
        owner().accounts.close()
        server.server_close()


if __name__ == "__main__":
    main()
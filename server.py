"""ChatGPT: the owner connects his ChatGPT subscription once; the agent on the same server uses it.

A Wizard AI connection app (contract wizard.ai-connection/1, decision #61). The sign-in is stock Hermes
Agent's own ChatGPT (Codex) device-code login, driven through that agent's dashboard API with the
dashboard login this app was installed with. Hermes creates, stores and refreshes the ChatGPT tokens
itself; Codex refresh tokens are single-use, so this app never sees, copies or holds one.

Usage numbers: read from the Wizard OS Hermes plugin's read-only dashboard route
(/api/plugins/wizard-os/ai-usage/chatgpt), so Hermes reads its own sign-in and this app never holds a token.
Without that plugin route `usage` stays null and the Agent Service keeps its own reader.
"""
from __future__ import annotations

import base64
import hmac
import http.cookiejar
import json
import os
import re
import secrets
import signal
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# Also supports the existing isolated (-I) launch used by the test harness.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import inference

VERSION = "0.2.0"
CONTRACT = "wizard.ai-connection/2"
APP = Path(__file__).resolve().parent
BIND = os.environ.get("WIZARD_CHATGPT_BIND", "0.0.0.0")
PORT = int(os.environ.get("WIZARD_CHATGPT_PORT", "8801"))
PASSWORD = os.environ.get("WIZARD_CHATGPT_PASSWORD", "")
# The agent's dashboard: by default the Hermes Agent app on the Runtipi main network.
HERMES_URL = (os.environ.get("WIZARD_CHATGPT_HERMES_URL", "") or "http://hermes-agent:9119").rstrip("/")
HERMES_USER = os.environ.get("WIZARD_CHATGPT_HERMES_USER", "") or "hermes"
HERMES_PASSWORD = os.environ.get("WIZARD_CHATGPT_HERMES_PASSWORD", "")
CODEX = "openai-codex"   # Hermes' provider id for a ChatGPT subscription
CHATGPT_HOSTS = {"auth.openai.com"}
CACHE_S = 60
URL = re.compile(r"https://[^\s\"'<>]+")
STATIC = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8"), "/SKILL.md": ("SKILL.md", "text/markdown; charset=utf-8")}
# Owner-data routes are box-only: the same port is published on the tailnet (Serve, tunnels and
# proxies add these headers), while agents and the Agent Service call the app on the app network.
PROXY_HEADERS = ("tailscale-user-login", "x-forwarded-for", "forwarded", "x-real-ip")
NO_AGENT = ("ChatGPT signs in inside the Hermes Agent app, and this app can't reach its dashboard. "
            "Install Hermes Agent from Wizard Apps, then reinstall ChatGPT. If your own Hermes runs on this "
            "server outside Wizard Apps, sign in there with `hermes auth add openai-codex`; Wizard still "
            "shows your ChatGPT usage from that sign-in.")


_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
FRAME_ANCESTORS = " ".join(["'self'"] + [o for o in (x.strip().rstrip("/") for x in
                                         os.environ.get("WIZARD_TRUSTED_ORIGINS", "").split(","))
                                         if _ORIGIN.match(o)])


class HermesError(Exception):
    def __init__(self, message: str, code: str = "provider_error"):
        super().__init__(message)
        self.code = code


class Hermes:
    """The agent's dashboard, signed in with its dashboard login (session cookies, kept in memory).
    Only the ChatGPT sign-in and model routes are called. Error text never carries a token."""
    lock = threading.Lock()
    opener = None

    @classmethod
    def configured(cls) -> bool:
        return bool(HERMES_PASSWORD)

    @classmethod
    def _login(cls):
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), inference.NoRedirect(), urllib.request.HTTPCookieProcessor(jar))
        body = json.dumps({"provider": "basic", "username": HERMES_USER, "password": HERMES_PASSWORD}).encode()
        req = urllib.request.Request(HERMES_URL + "/auth/password-login", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=20) as res:
                res.read()
        except urllib.error.HTTPError as e:
            raise HermesError("Hermes Agent did not accept this app's dashboard login. Reinstall ChatGPT so it "
                              "gets the Hermes Agent dashboard login." if e.code in (401, 403, 404)
                              else f"Hermes Agent answered {e.code}.", "provider_error") from None
        except OSError:
            raise HermesError(NO_AGENT, "unreachable") from None
        cls.opener = opener

    @classmethod
    def call(cls, method: str, path: str, body: dict | None = None, timeout: int = 40) -> dict:
        if not cls.configured():
            raise HermesError(NO_AGENT, "unreachable")
        for attempt in (0, 1):
            with cls.lock:
                if cls.opener is None:
                    cls._login()
                opener = cls.opener
            data = None if body is None else json.dumps(body).encode()
            req = urllib.request.Request(HERMES_URL + path, data=data, method=method,
                                         headers={"Content-Type": "application/json"} if data is not None else {})
            try:
                with opener.open(req, timeout=timeout) as res:
                    return json.loads(res.read() or b"{}")
            except urllib.error.HTTPError as e:
                if e.code == 404 and path == inference.MODELS_PATH:
                    e.close()
                    raise HermesError(inference.REQUIRED) from None
                if e.code == 401 and attempt == 0:   # the dashboard session expired: sign in once more
                    with cls.lock:
                        cls.opener = None
                    continue
                try:
                    detail = str(json.loads(e.read() or b"{}").get("detail") or "")
                except (ValueError, AttributeError):
                    detail = ""
                raise HermesError(URL.sub("[link]", detail)[:200] or f"Hermes Agent answered {e.code}.") from None
            except (OSError, ValueError):
                raise HermesError(NO_AGENT, "unreachable") from None
        raise HermesError("Hermes Agent did not accept this app's dashboard login.")


def _label(status: dict) -> str | None:
    """An email-shaped account label, if Hermes' status names one (pool rows are labelled `pool:<label>`)."""
    source = str(status.get("source") or "")
    for value in (status.get("email"), status.get("account_email"), source[5:] if source.startswith("pool:") else None):
        if isinstance(value, str) and "@" in value and len(value) <= 120:
            return value
    return None


def chatgpt_status() -> dict:
    """Connected or not (from Hermes' own sign-in status) and whether the agent uses it now. No token data."""
    try:
        rows = Hermes.call("GET", "/api/providers/oauth").get("providers") or []
        row = next((p for p in rows if isinstance(p, dict) and p.get("id") == CODEX), None)
        info = Hermes.call("GET", "/api/model/info")
    except HermesError as e:
        return {"status": "error", "code": e.code, "detail": str(e)}
    if row is None:
        return {"status": "error", "code": "provider_error", "detail": "This Hermes Agent has no ChatGPT sign-in."}
    in_use = info.get("provider") == CODEX
    status = row.get("status") or {}
    if not status.get("logged_in"):
        return {"status": "not_connected", "in_use": in_use}
    return {"status": "connected", "account": _label(status), "in_use": in_use,
            "model": str(info.get("model") or "")[:80] if in_use else ""}


USAGE_PATH = "/api/plugins/wizard-os/ai-usage/chatgpt"


def chatgpt_usage() -> tuple[str | None, dict | None]:
    """(plan, usage) from the Wizard OS plugin's read-only route on the agent's dashboard: Hermes reads its own
    ChatGPT usage, so this app still never holds a token. (None, None) when the route is missing (an agent
    without the plugin) or the read fails; the Agent Service then keeps its own reader."""
    try:
        d = Hermes.call("GET", USAGE_PATH, timeout=30)
    except HermesError:
        return None, None
    usage = d.get("usage") if d.get("ok") is True else None
    if not isinstance(usage, dict) or not isinstance(usage.get("limits"), list):
        return None, None
    plan = d.get("plan")
    return (str(plan)[:40] if isinstance(plan, str) and plan else None), {
        "fetchedAt": usage.get("fetchedAt"), "limits": usage["limits"][:20], "paid": usage.get("paid")}


class Cache:
    lock = threading.Lock()
    at = 0.0
    value: dict | None = None


def connection(fresh: bool = False) -> dict:
    """The wizard.ai-connection/1 answer, cached 60 s (connect/disconnect clear it)."""
    with Cache.lock:
        if not fresh and Cache.value is not None and time.monotonic() - Cache.at < CACHE_S:
            return Cache.value
    s = chatgpt_status()
    out = {"contract": CONTRACT, "provider": "chatgpt", "name": "ChatGPT", "status": s["status"],
           "plan": None, "account": s.get("account") if s["status"] == "connected" else None,
           "usage": None, "error": None}
    if s["status"] == "connected":
        plan, usage = chatgpt_usage()
        out["plan"], out["usage"] = plan, usage
    if s["status"] == "not_connected":
        out["error"] = {"code": "signed_out", "message": "ChatGPT is not connected. Open the ChatGPT app and press Connect."}
    elif s["status"] == "error":
        out["error"] = {"code": s.get("code") or "provider_error", "message": s.get("detail") or "Something is wrong."}
    try:
        out["models"] = inference.models(Hermes)
    except (HermesError, inference.Fault) as e:
        out["models"] = []
        # Preserve /1 status/usage; model discovery failure never invents availability.
        if out["error"] is None:
            out["models_error"] = {"code": e.code, "message": str(e)}
    with Cache.lock:
        Cache.value, Cache.at = out, time.monotonic()
    return out


def forget():
    with Cache.lock:
        Cache.value = None


def use_chatgpt(force: bool) -> bool:
    """Point the agent's main model at ChatGPT. Without force, only when its current account doesn't work,
    so connecting ChatGPT never silently moves an agent off a working account. True when switched."""
    rows = Hermes.call("GET", "/api/model/options?include_unconfigured=true", timeout=90).get("providers") or []
    current = next((p for p in rows if p.get("is_current")), None)
    if current and current.get("slug") == CODEX:
        return False
    if not force and current and current.get("authenticated"):
        return False
    codex = next((p for p in rows if p.get("slug") == CODEX), {})
    models = [m for m in (codex.get("featured_models") or []) + (codex.get("models") or []) if isinstance(m, str)]
    if not models:
        raise HermesError("ChatGPT is connected, but Hermes lists no ChatGPT models yet. Press Use ChatGPT in a minute.")
    res = Hermes.call("POST", "/api/model/set", {"scope": "main", "provider": CODEX, "model": models[0],
                                                 "confirm_expensive_model": True}, timeout=90)
    if res.get("ok") is False:
        raise HermesError(str(res.get("confirm_message") or res.get("detail") or "Hermes did not switch the model.")[:200])
    return True


class Login:
    lock = threading.Lock()
    session = ""     # Hermes' sign-in session id (never sent to the browser)
    login_id = ""    # this page's handle for it


def _end_login():
    sid, Login.session, Login.login_id = Login.session, "", ""
    if sid:
        try:
            Hermes.call("DELETE", f"/api/providers/oauth/sessions/{sid}")
        except HermesError:
            pass


def connect() -> tuple[int, dict]:
    with Login.lock:
        _end_login()
        try:
            d = Hermes.call("POST", f"/api/providers/oauth/{CODEX}/start", {}, timeout=40)
        except HermesError as e:
            return 502, {"ok": False, "message": str(e)}
        url, code, sid = str(d.get("verification_url") or ""), str(d.get("user_code") or ""), str(d.get("session_id") or "")
        parts = urlsplit(url)
        if not sid or parts.scheme != "https" or parts.hostname not in CHATGPT_HOSTS \
                or not re.fullmatch(r"[A-Za-z0-9-]{4,24}", code):
            Login.session = sid
            _end_login()
            return 502, {"ok": False, "message": "ChatGPT did not give a sign-in code. Try again in a minute."}
        Login.session, Login.login_id = sid, secrets.token_urlsafe(16)
        return 200, {"ok": True, "login_id": Login.login_id, "url": url, "code": code,
                     "expires_in": int(d.get("expires_in") or 900)}


def poll(login_id: str) -> tuple[int, dict]:
    with Login.lock:
        if not Login.login_id or not hmac.compare_digest(login_id.encode(), Login.login_id.encode()):
            return 409, {"ok": False, "status": "expired", "message": "This sign-in expired. Press Connect again."}
        sid = Login.session
    try:
        d = Hermes.call("GET", f"/api/providers/oauth/{CODEX}/poll/{sid}")
    except HermesError as e:
        with Login.lock:
            Login.session = Login.login_id = ""
        return 409, {"ok": False, "status": "expired", "message": "This sign-in expired. Press Connect again."
                     if "not found" in str(e).lower() else str(e)}
    status = d.get("status")
    if status == "pending":
        return 200, {"ok": True, "status": "pending"}
    with Login.lock:
        Login.session = Login.login_id = ""
    forget()
    if status != "approved":
        return 400, {"ok": False, "status": status or "error",
                     "message": "The code expired before it was approved. Press Connect again." if status == "expired"
                     else "ChatGPT sign-in did not finish. Press Connect to try again."}
    try:
        switched = use_chatgpt(force=False)
        message = ("Connected. Your agent now uses ChatGPT." if switched else
                   "Connected. Your agent keeps its current account; press Use ChatGPT to switch.")
    except HermesError as e:
        message = f"Connected, but the agent was not switched: {e}"
    return 200, {"ok": True, "status": "approved", "message": message}


def cancel() -> tuple[int, dict]:
    with Login.lock:
        _end_login()
    return 200, {"ok": True}


def use() -> tuple[int, dict]:
    if chatgpt_status().get("status") != "connected":
        return 409, {"ok": False, "message": "Connect ChatGPT first."}
    try:
        switched = use_chatgpt(force=True)
    except HermesError as e:
        return 502, {"ok": False, "message": str(e)}
    return 200, {"ok": True, "message": "Your agent now uses ChatGPT for new chats." if switched
                 else "Your agent already uses ChatGPT."}


def logout() -> tuple[int, dict]:
    with Login.lock:
        _end_login()
    before = chatgpt_status()
    try:
        Hermes.call("DELETE", f"/api/providers/oauth/{CODEX}")
    except HermesError as e:
        return 502, {"ok": False, "message": str(e)}
    forget()
    status = chatgpt_status()
    ok = status.get("status") == "not_connected"
    message = "Disconnected." if ok else "Still connected."
    if ok and before.get("in_use"):
        message = "Disconnected. Your agent was using ChatGPT: connect it again, or pick another model."
    return (200 if ok else 500), {"ok": ok, "message": message}


def account_row() -> dict:
    s = chatgpt_status()
    row = {"id": "chatgpt-subscription", "name": "ChatGPT subscription", "kind": "subscription",
           "detail": "Plus or Pro plan, signed in inside your Hermes Agent with ChatGPT's own device code.",
           "status": s["status"], "in_use": bool(s.get("in_use")), "account": s.get("account") or "",
           "model": s.get("model") or "", "signing_in": bool(Login.login_id)}
    if s["status"] == "error":
        row["problem"] = s.get("detail") or ""
    return row


def card(host: str) -> dict:
    base = f"http://{host}" if host else f"http://wizard-chatgpt:{PORT}"
    return {
        "name": "wizard-chatgpt", "version": VERSION, "title": "ChatGPT",
        "description": "Connect your ChatGPT subscription once. The sign-in runs inside your Hermes Agent, "
                       "which keeps the tokens; this app only starts it and reports the status.",
        "remotes": [],
        "x-wizard": {
            "kind": "system-agent-app",
            "provides": ["ai-connection", "chatgpt-subscription"],
            "contract": CONTRACT,
            "skill": f"{base}/SKILL.md",
            "connection_api": f"{base}/api/connection",
            "agent_dashboard": "configured" if Hermes.configured() else "missing",
            "usage": "from the agent's dashboard (Wizard OS plugin route) when present",
            "ai_calls": True,
            "inference_api": "router-only /api/infer (Hermes wizard-os >=0.4.0)",
            "secrets_exposed": False,
        },
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "wizard-chatgpt"
    sys_version = ""

    def log_message(self, format, *args):  # noqa: A002 - request lines only; never bodies
        sys.stderr.write("%s %s\n" % (self.command, urlsplit(self.path).path))

    def send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # Framing: only this page itself and the Wizard shell origins the App Manager trusts (WIZARD_TRUSTED_ORIGINS),
        # so the page opens inside a Wizard window and no other site can frame its Connect buttons.
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; "
                         "img-src 'self' data:; connect-src 'self'; frame-ancestors " + FRAME_ANCESTORS +
                         "; base-uri 'none'; form-action 'none'")
        if FRAME_ANCESTORS == "'self'":
            self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, code: int, data: dict):
        self.send(code, json.dumps(data).encode(), "application/json")

    def authorized(self, path: str) -> bool:
        if path == "/api/connection" and self.headers.get("X-Wizard-Grant"):
            try:
                if inference.admit(self.headers):
                    return True
            except inference.Fault as e:
                self.json(e.status, e.payload())
                return False
        if not PASSWORD or path == "/health":
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
                if hmac.compare_digest(given.encode(), PASSWORD.encode()):
                    return True
            except (ValueError, UnicodeDecodeError):
                pass
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="ChatGPT", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def proxied(self) -> bool:
        return any(k.lower() in PROXY_HEADERS or k.lower().startswith("cf-") for k in self.headers.keys())

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if not self.authorized(path):
            return
        if path == "/health":
            return self.json(200, {"status": "ok", "version": VERSION})
        if path == "/.well-known/mcp/server-cards.json":
            return self.json(200, card(self.headers.get("Host", "")))
        if path == "/api/connection":
            if self.proxied():
                return self.json(403, {"error": "box-only: call this from the server (app network or 127.0.0.1)"})
            return self.json(200, connection())
        if path == "/api/accounts":
            return self.json(200, {"accounts": [account_row()], "version": VERSION})
        if path in STATIC:
            name, ctype = STATIC[path]
            return self.send(200, (APP / "static" / name).read_bytes() if name != "SKILL.md" else
                             (APP / "SKILL.md").read_bytes(), ctype)
        self.json(404, {"error": "not found"})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/infer":
            return inference.handle(self, Hermes, HERMES_URL)
        if not self.authorized(path):
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self.json(403, {"ok": False, "message": "Cross-site request refused."})
        if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
            return self.json(415, {"ok": False, "message": "JSON only."})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 4096:
            return self.json(413, {"ok": False, "message": "Too large."})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self.json(400, {"ok": False, "message": "Bad request."})
        routes = {"/api/chatgpt/connect": connect, "/api/chatgpt/cancel": cancel,
                  "/api/chatgpt/use": use, "/api/chatgpt/logout": logout}
        if path == "/api/chatgpt/poll":
            return self.json(*poll(str(body.get("login_id", ""))))
        if path in routes:
            return self.json(*routes[path]())
        self.json(404, {"ok": False, "message": "not found"})


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):  # skip getfqdn(): it stalls without a resolver
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = str(self.server_address[0]), self.server_address[1]


def main():
    server = Server((BIND, PORT), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown, daemon=True).start())
    print(f"ChatGPT {VERSION} on {BIND}:{PORT} (agent dashboard {'configured' if Hermes.configured() else 'missing'})",
          flush=True)
    try:
        server.serve_forever()
    finally:
        with Login.lock:
            _end_login()
        server.server_close()


if __name__ == "__main__":
    main()

"""ChatGPT app, end to end against a stand-in Hermes Agent dashboard (no network, no real account).

Runs server.py as a subprocess pointed at a fake Hermes that speaks the same routes stock Hermes Agent's
dashboard serves (/auth/password-login, /api/providers/oauth..., /api/model/...). Proves the
wizard.ai-connection/1 answer (not_connected, connected with usage null, error when unreachable), the
box-only refusal, the 60 s cache and its reset on sign-in changes, the device-code flow (connect, a 401
re-login, approve with and without a working agent account, Use ChatGPT, disconnect, cancel, expired),
cross-site refusal, and that no token, dashboard password, cookie or Hermes session id reaches any response.
Usage: python3 test-chatgpt.py
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN = "tok-SECRET-never-shown"
CONTRACT_KEYS = ["contract", "provider", "name", "status", "plan", "account", "usage", "error", "models"]


class Fake:
    logged_in = False
    current = "claude-subscription-directsdk-experimental"
    current_ok = True
    sessions: dict = {}
    calls: list = []
    expire_cookie_once = False
    model_set: list = []
    usage_route = False   # the Wizard OS plugin route (absent on an agent without the plugin)


class Stub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, data, cookie=None):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        if Fake.expire_cookie_once:
            Fake.expire_cookie_once = False
            return False
        return "hermes_session_at=ok" in (self.headers.get("Cookie") or "")

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def route(self, method):
        path = self.path
        Fake.calls.append(f"{method} {path.split('?')[0]}")
        if method == "POST" and path == "/auth/password-login":
            b = self.body()
            if b == {"provider": "basic", "username": "hermes", "password": "dash-pass"}:
                return self.reply(200, {"ok": True}, "hermes_session_at=ok; Path=/; HttpOnly")
            return self.reply(401, {"detail": "Invalid credentials"})
        if not self.authed():
            return self.reply(401, {"detail": "unauthenticated"})
        if method == "GET" and path == "/api/providers/oauth":
            st = {"logged_in": Fake.logged_in, "token_preview": TOKEN[-4:]}
            if Fake.logged_in:
                st["source"] = "pool:owner@example.com"
            return self.reply(200, {"providers": [{"id": "openai-codex", "status": st}]})
        if method == "GET" and path == "/api/plugins/wizard-os/ai-usage/chatgpt":
            if not Fake.usage_route:
                return self.reply(404, {"detail": "Not Found"})
            return self.reply(200, {"ok": True, "plan": "Pro", "usage": {
                "fetchedAt": "2026-10-16T04:48:23.362Z", "paid": None,
                "limits": [{"id": "weekly:0", "label": "Weekly", "usedPercent": 6.0, "remainingPercent": 94,
                            "resetsAt": "2026-10-22T06:13:23.000Z"}]}})
        if method == "GET" and path == "/api/plugins/wizard-os/models/chatgpt":
            return self.reply(200, {"models": [{"id": "gpt-6.1-sol", "label": "Sol", "tier": "standard", "abilities": ["text", "streaming"]}]})
        if method == "GET" and path == "/api/model/info":
            return self.reply(200, {"provider": Fake.current, "model": "gpt-6-sol" if Fake.current == "openai-codex" else "opus"})
        if method == "GET" and path.startswith("/api/model/options"):
            return self.reply(200, {"providers": [
                {"slug": "claude-subscription-directsdk-experimental", "is_current": Fake.current != "openai-codex",
                 "authenticated": Fake.current_ok, "models": ["opus"], "featured_models": []},
                {"slug": "openai-codex", "is_current": Fake.current == "openai-codex", "authenticated": Fake.logged_in,
                 "models": ["gpt-6-luna", "gpt-6-sol"] if Fake.logged_in else [], "featured_models": ["gpt-6-sol"] if Fake.logged_in else []}]})
        if method == "POST" and path == "/api/model/set":
            b = self.body()
            Fake.model_set.append(b)
            Fake.current = b["provider"]
            return self.reply(200, {"ok": True})
        if method == "POST" and path == "/api/providers/oauth/openai-codex/start":
            sid = f"s{len(Fake.sessions)}"
            Fake.sessions[sid] = "pending"
            return self.reply(200, {"session_id": sid, "flow": "device_code", "user_code": "ABCD-1234",
                                    "verification_url": "https://auth.openai.com/codex/device", "expires_in": 900})
        if method == "GET" and path.startswith("/api/providers/oauth/openai-codex/poll/"):
            sid = path.rsplit("/", 1)[1]
            if sid not in Fake.sessions:
                return self.reply(404, {"detail": "Session not found or expired"})
            return self.reply(200, {"session_id": sid, "status": Fake.sessions[sid]})
        if method == "DELETE" and path.startswith("/api/providers/oauth/sessions/"):
            Fake.sessions.pop(path.rsplit("/", 1)[1], None)
            return self.reply(200, {"ok": True})
        if method == "DELETE" and path == "/api/providers/oauth/openai-codex":
            Fake.logged_in = False
            return self.reply(200, {"ok": True, "provider": "openai-codex"})
        return self.reply(404, {"detail": "no route"})

    def do_GET(self):
        self.route("GET")

    def do_POST(self):
        self.route("POST")

    def do_DELETE(self):
        self.route("DELETE")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start(env_extra):
    port = free_port()
    env = {"PATH": os.environ["PATH"], "WIZARD_CHATGPT_PORT": str(port), "WIZARD_CHATGPT_BIND": "127.0.0.1", **env_extra}
    proc = subprocess.Popen([sys.executable, "-I", str(HERE / "server.py")], env=env)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            urllib.request.urlopen(base + "/health", timeout=2).read()
            break
        except OSError:
            time.sleep(0.1)
    return proc, base


def main():
    stub = ThreadingHTTPServer(("127.0.0.1", 0), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    proc, base = start({"WIZARD_CHATGPT_HERMES_URL": f"http://127.0.0.1:{stub.server_port}",
                        "WIZARD_CHATGPT_HERMES_USER": "hermes", "WIZARD_CHATGPT_HERMES_PASSWORD": "dash-pass"})
    seen = []

    def req(path, body=None, headers=None):
        h = {"Content-Type": "application/json"} if body is not None else {}
        h.update(headers or {})
        r = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(r, timeout=30) as res:
                raw, code = res.read(), res.status
        except urllib.error.HTTPError as e:
            raw, code = e.read(), e.code
        seen.append(raw.decode())
        return code, json.loads(raw)

    def row():
        return req("/api/accounts")[1]["accounts"][0]

    try:
        # Logged out: the contract answer, signed_out, no usage.
        code, conn = req("/api/connection")
        assert code == 200 and list(conn) == CONTRACT_KEYS, conn
        assert conn["contract"] == "wizard.ai-connection/2" and conn["provider"] == "chatgpt" and conn["name"] == "ChatGPT"
        assert conn["status"] == "not_connected" and conn["usage"] is None and conn["plan"] is None
        assert conn["error"]["code"] == "signed_out", conn
        assert conn["models"][0]["id"] == "gpt-6.1-sol"
        assert row()["status"] == "not_connected"

        # Box-only: every proxy header is refused, the page stays open.
        for h in ({"Tailscale-User-Login": "x@y"}, {"X-Forwarded-For": "1.2.3.4"}, {"Forwarded": "for=1.2.3.4"},
                  {"X-Real-IP": "1.2.3.4"}, {"CF-Connecting-IP": "1.2.3.4"}, {"cf-ray": "abc"}):
            code, d = req("/api/connection", None, h)
            assert code == 403 and "box-only" in d["error"] and "status" not in d, (h, d)
        r = urllib.request.Request(base + "/", headers={"Tailscale-User-Login": "x@y"})
        assert urllib.request.urlopen(r, timeout=10).status == 200

        # The answer is cached for 60 s: a sign-in made elsewhere shows on the next uncached read.
        n = len(Fake.calls)
        req("/api/connection")
        assert len(Fake.calls) == n, "second read inside 60 s must come from the cache"

        # Use before connecting is refused.
        assert req("/api/chatgpt/use", {})[0] == 409 and not Fake.model_set

        # Connect -> code + OpenAI link; Hermes' session id never leaves the server.
        code, d = req("/api/chatgpt/connect", {})
        assert code == 200 and d["code"] == "ABCD-1234" and d["url"] == "https://auth.openai.com/codex/device", d
        assert "session_id" not in d and "s0" not in json.dumps(d), d
        assert row()["signing_in"] is True
        assert req("/api/chatgpt/poll", {"login_id": "stale"})[0] == 409
        assert req("/api/chatgpt/poll", {"login_id": d["login_id"]})[1]["status"] == "pending"

        # The dashboard session expires mid-way: the app signs in again and carries on.
        Fake.expire_cookie_once = True
        assert req("/api/chatgpt/poll", {"login_id": d["login_id"]})[1]["status"] == "pending"

        # Approved while the agent's other account works: connected, NOT switched; the cache is cleared.
        # Without the Wizard OS plugin route on the agent's dashboard, usage stays null (checked below).
        Fake.sessions["s0"], Fake.logged_in = "approved", True
        code, d3 = req("/api/chatgpt/poll", {"login_id": d["login_id"]})
        assert code == 200 and d3["status"] == "approved" and "keeps its current account" in d3["message"], d3
        assert not Fake.model_set and Fake.current != "openai-codex"
        code, conn = req("/api/connection")
        assert conn["status"] == "connected" and conn["usage"] is None and conn["error"] is None, conn
        assert conn["account"] == "owner@example.com" and conn["plan"] is None, conn

        c = row()
        assert c["status"] == "connected" and c["in_use"] is False and not c["signing_in"], c

        # Use ChatGPT switches the main model to the first featured ChatGPT model (owner's button only).
        code, d4 = req("/api/chatgpt/use", {})
        assert code == 200 and d4["ok"], d4
        assert Fake.model_set[-1] == {"scope": "main", "provider": "openai-codex", "model": "gpt-6-sol",
                                      "confirm_expensive_model": True}, Fake.model_set
        assert row()["in_use"] is True

        # Disconnect: signed out, the owner is told the agent was using it, the contract follows at once.
        code, d5 = req("/api/chatgpt/logout", {})
        assert code == 200 and "was using ChatGPT" in d5["message"], d5
        assert req("/api/connection")[1]["status"] == "not_connected"

        # A fresh agent with no working account: approving switches it automatically.
        Fake.current, Fake.current_ok, Fake.model_set = "claude-subscription-directsdk-experimental", False, []
        d = req("/api/chatgpt/connect", {})[1]
        Fake.sessions[max(Fake.sessions)], Fake.logged_in = "approved", True
        d6 = req("/api/chatgpt/poll", {"login_id": d["login_id"]})[1]
        assert "now uses ChatGPT" in d6["message"] and Fake.current == "openai-codex", d6

        # With the Wizard OS plugin route, plan and usage come from the agent's own read; still no token here.
        Fake.usage_route = True
        code, conn = req("/api/connection")
        assert conn["status"] == "connected" and conn["plan"] == "Pro" and conn["error"] is None, conn
        assert conn["usage"]["limits"][0] == {"id": "weekly:0", "label": "Weekly", "usedPercent": 6.0,
                                              "remainingPercent": 94, "resetsAt": "2026-10-22T06:13:23.000Z"}, conn
        assert "GET /api/plugins/wizard-os/ai-usage/chatgpt" in Fake.calls
        Fake.usage_route = False

        # Cancel ends Hermes' own session too.
        Fake.logged_in = False
        d = req("/api/chatgpt/connect", {})[1]
        sid = max(Fake.sessions)
        assert req("/api/chatgpt/cancel", {})[1]["ok"] and sid not in Fake.sessions
        assert req("/api/chatgpt/poll", {"login_id": d["login_id"]})[0] == 409

        # Expired code: a clear message, nothing switched.
        Fake.current, Fake.model_set = "claude-subscription-directsdk-experimental", []
        d = req("/api/chatgpt/connect", {})[1]
        Fake.sessions[max(Fake.sessions)] = "expired"
        code, d7 = req("/api/chatgpt/poll", {"login_id": d["login_id"]})
        assert code == 400 and "expired" in d7["message"] and not Fake.model_set, d7

        # Cross-site and non-JSON posts are refused.
        for hdr in ({"Content-Type": "application/json", "Origin": "http://evil.example"}, {"Content-Type": "text/plain"}):
            r = urllib.request.Request(base + "/api/chatgpt/use", data=b"{}", headers=hdr)
            try:
                urllib.request.urlopen(r, timeout=10)
                raise AssertionError("post accepted")
            except urllib.error.HTTPError as e:
                assert e.code in (403, 415), e.code

        # No credential surface.
        for path in ("/auth.json", "/server.py", "/static/../server.py", "/api/chatgpt/token"):
            assert req(path)[0] == 404, path
        code, cardj = req("/.well-known/mcp/server-cards.json")
        assert cardj["x-wizard"]["provides"] == ["ai-connection", "chatgpt-subscription"], cardj
        assert cardj["x-wizard"]["agent_dashboard"] == "configured"

        everything = "\n".join(seen)
        for secret in (TOKEN, TOKEN[-4:], "dash-pass", "hermes_session_at", '"s0"', '"s1"'):
            assert secret not in everything, secret
        assert "/auth/password-login" in " ".join(Fake.calls)
        print(json.dumps({"ok": True, "hermes_calls": len(Fake.calls), "browser_responses": len(seen)}))
    finally:
        proc.terminate()
        proc.wait(10)
        stub.shutdown()


def unreachable():
    """No dashboard login (alongside box) or a dead dashboard: status error / unreachable, a plain message."""
    for extra in ({}, {"WIZARD_CHATGPT_HERMES_URL": "http://127.0.0.1:9", "WIZARD_CHATGPT_HERMES_PASSWORD": "x" * 20}):
        proc, base = start(extra)
        try:
            conn = json.loads(urllib.request.urlopen(base + "/api/connection", timeout=15).read())
            assert list(conn) == CONTRACT_KEYS and conn["status"] == "error" and conn["usage"] is None, conn
            assert conn["error"]["code"] == "unreachable" and "Hermes Agent" in conn["error"]["message"], conn
            rows = json.loads(urllib.request.urlopen(base + "/api/accounts", timeout=15).read())["accounts"]
            assert rows[0]["status"] == "error" and "Hermes Agent" in rows[0]["problem"], rows
            r = urllib.request.Request(base + "/api/chatgpt/connect", data=b"{}", headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(r, timeout=15)
                raise AssertionError("connect without a dashboard succeeded")
            except urllib.error.HTTPError as e:
                assert e.code == 502, e.code
        finally:
            proc.terminate()
            proc.wait(10)
    print(json.dumps({"ok": True, "unreachable": "error/unreachable"}))


if __name__ == "__main__":
    main()
    unreachable()

"""Offline OAuth, rotation, migration and gateway integration tests. No live credentials."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs
from urllib.request import Request, urlopen

import codex
import server


def jwt(exp=None):
    b64 = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return b64({"alg": "none"}) + "." + b64({"exp": exp or time.time() + 3600,
        "https://api.openai.com/auth": {"chatgpt_account_id": "FAKE-ACCOUNT-PRIVATE", "chatgpt_plan_type": "pro"}}) + ".fake"


@contextmanager
def serving(s):
    t = threading.Thread(target=s.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
    t.start()
    try:
        yield "http://127.0.0.1:" + str(s.server_port)
    finally:
        s.shutdown()
        s.server_close()
        t.join(5)


class Fake(BaseHTTPRequestHandler):
    state = {}
    def log_message(self, *args):
        pass

    def reply(self, status, obj):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        s = self.state
        s["requests"].append((self.command, self.path, dict(self.headers), None))
        if s["mode"] == "unauthorized":
            return self.reply(401, {"error": "temporary backend rejection"})
        if self.path.startswith("/backend-api/codex/models"):
            return self.reply(200, {"models": [{"slug": "gpt-5.6-sol", "display_name": "GPT 5.6"}]})
        if self.path == "/backend-api/wham/usage":
            return self.reply(200, {"plan_type": "pro", "rate_limit": {"primary_window": {"used_percent": 25, "limit_window_seconds": 18000, "reset_at": 1900000000}}})
        if self.path.endswith("rate-limit-reset-credits"):
            return self.reply(200, {"available": 0})
        self.reply(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = parse_qs(raw.decode()) if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded") else json.loads(raw or b"{}")
        s = self.state
        with s["lock"]:
            s["requests"].append((self.command, self.path, dict(self.headers), body))
        if self.path == "/api/accounts/deviceauth/usercode":
            return self.reply(200, {"user_code": "ABCD-1234", "device_auth_id": "PRIVATE-DEVICE", "interval": "3"})
        if self.path == "/api/accounts/deviceauth/token":
            s["polls"] += 1
            if s["polls"] == 1:
                return self.reply(403, {"error": "authorization_pending"})
            return self.reply(200, {"authorization_code": "PRIVATE-CODE", "code_verifier": "PRIVATE-VERIFIER"})
        if self.path == "/oauth/token":
            assert body["client_id"] == [codex.CLIENT_ID]
            if body["grant_type"] == ["authorization_code"]:
                assert body["redirect_uri"][0].endswith("/deviceauth/callback")
                return self.reply(200, {"access_token": jwt(), "refresh_token": s["refresh"]})
            with s["lock"]:
                s["refreshes"] += 1
                if s["invalid"] or body["refresh_token"] != [s["refresh"]]:
                    return self.reply(401 if s.get("invalid_code") else 400, {"error": {"code": s["invalid_code"]} if s.get("invalid_code") else "invalid_grant", "detail": jwt()})
                s["refresh"] = "FAKE-ROTATED-" + str(s["refreshes"])
            time.sleep(.04)
            return self.reply(200, {"access_token": jwt(), "refresh_token": s["refresh"]})
        if self.path == "/backend-api/codex/responses/compact":
            return self.reply(200, {"object": "response.compaction", "output": [{"type": "compaction", "encrypted_content": "fake-encrypted"}], "usage": {"input_tokens": 10, "output_tokens": 2}})
        if self.path == "/backend-api/codex/responses":
            if s["mode"] == "error":
                return self.reply(400, {"error": jwt(), "refresh_token": s["refresh"]})
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Set-Cookie", "private=never-forward")
            self.send_header("x-codex-primary-used-percent", "25")
            self.end_headers()
            events = [{"type": "response.created", "response": {"id": "resp_fake", "object": "response", "status": "in_progress", "output": []}},
                      {"type": "response.output_text.delta", "delta": "hello", "item_id": "msg_fake", "output_index": 0, "content_index": 0},
                      {"type": "response.completed", "response": {"id": "resp_fake", "object": "response", "status": "completed", "model": "wrong" if s["mode"] == "mismatch" else body["model"], "output": [{"type": "message", "id": "msg_fake", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": "hello", "annotations": []}]}], "usage": {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5}}}]
            for i, e in enumerate(events):
                if i == 2 and s["mode"] == "missing":
                    break
                line = ("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n").encode()
                self.wfile.write(line)
                self.wfile.flush()
                if i == 1 and s["mode"] == "slow":
                    s["delta_sent"].set()
                    s["continue"].wait(4)
            return
        self.reply(404, {})


class AppCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = io.StringIO()
        self.out = redirect_stdout(self.log); self.out.__enter__()
        self.err = redirect_stderr(self.log); self.err.__enter__()
        self.state = {"lock": threading.Lock(), "requests": [], "polls": 0, "refreshes": 0,
                      "refresh": "FAKE-INITIAL-REFRESH", "invalid": False, "mode": "ok", "delta_sent": threading.Event(), "continue": threading.Event()}
        self.fake = type("CaseFake", (Fake,), {"state": self.state})
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), self.fake)
        self.bctx = serving(self.backend); self.upstream = self.bctx.__enter__()
        self.conn = codex.Connection(Path(self.tmp.name) / "app-data", self.upstream, self.upstream + "/backend-api")
        self.settings = patch.multiple(server, OWNER=self.conn, PASSWORD="", HERMES_PASSWORD="")
        self.settings.start()
        self.env = patch.dict(os.environ, {"WIZARD_GRANTS_FILE": ""})
        self.env.start()
        self.srv = server.Server(("127.0.0.1", 0), server.Handler)
        self.sctx = serving(self.srv); self.base = self.sctx.__enter__()

    def tearDown(self):
        self.state["continue"].set()
        self.sctx.__exit__(None, None, None); self.bctx.__exit__(None, None, None)
        self.settings.stop(); self.env.stop()
        self.err.__exit__(None, None, None); self.out.__exit__(None, None, None)
        for secret in (jwt(), self.state["refresh"], "PRIVATE-DEVICE", "PRIVATE-VERIFIER", "FAKE-ACCOUNT-PRIVATE"):
            self.assertNotIn(secret, self.log.getvalue())
        self.tmp.cleanup()

    def call(self, path, body=None, headers=None):
        req = Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
                      headers={"Content-Type": "application/json", **(headers or {})})
        try:
            r = urlopen(req, timeout=8)
        except HTTPError as e:
            r = e
        with r:
            return r.status, r.read(), dict(r.headers)

    def login(self):
        d = self.conn.connect()
        self.assertEqual(self.conn.poll(d["login_id"])["status"], "pending")
        self.conn.login["next"] = 0
        self.assertEqual(self.conn.poll(d["login_id"])["status"], "approved")

    def seed(self, exp=None):
        with self.conn.lock():
            self.conn.atomic("connection.json", {"status": "connected", "access_token": jwt(exp), "refresh_token": self.state["refresh"], "expires_at": exp or time.time() + 3600})

    def auth(self):
        return {"Authorization": "Bearer " + self.conn.gateway_token()}


class OAuthTests(AppCase):
    def test_device_exchange_ui_api_status_and_disconnect(self):
        status, raw, _ = self.call("/api/chatgpt/connect", {}, {"X-Wizard-Request": "1"})
        self.assertEqual(status, 200)
        d = json.loads(raw)
        self.assertNotIn("PRIVATE-DEVICE", raw.decode())
        self.assertEqual(d["url"], "https://auth.openai.com/codex/device")
        self.call("/api/chatgpt/poll", {"login_id": d["login_id"]}, {"X-Wizard-Request": "1"})
        self.conn.login["next"] = 0
        status, raw, _ = self.call("/api/chatgpt/poll", {"login_id": d["login_id"]}, {"X-Wizard-Request": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["status"], "approved")
        status, raw, _ = self.call("/api/connection")
        d = json.loads(raw)
        self.assertEqual((status, d["contract"], d["status"], d["plan"]), (200, "wizard.ai-connection/2", "connected", "pro"))
        self.assertEqual(d["usage"]["limits"][0]["remainingPercent"], 75)
        for secret in (self.conn.stored()["access_token"], self.state["refresh"], "FAKE-ACCOUNT-PRIVATE"):
            self.assertNotIn(secret, raw.decode())
        # The Agent Service reads this with Node's fetch, which always adds Sec-Fetch-Mode: cors.
        self.assertEqual(self.call("/api/connection", headers={"Sec-Fetch-Mode": "cors"})[0], 200)
        self.assertEqual(self.call("/api/connection", headers={"Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"})[0], 403)
        self.assertEqual(self.call("/api/chatgpt/test", {}, {"X-Wizard-Request": "1"})[0], 200)
        self.assertEqual(self.call("/api/chatgpt/logout", {}, {"X-Wizard-Request": "1"})[0], 200)
        self.assertEqual(self.conn.stored(), {})
        self.assertEqual(self.call("/v1/models", headers=self.auth())[0], 401)

    def test_skew_rotation_atomic_modes_and_singleflight_across_instances(self):
        self.seed(time.time() + 60)
        other = codex.Connection(self.conn.data, self.upstream, self.upstream + "/backend-api")
        with ThreadPoolExecutor(max_workers=12) as pool:
            tokens = list(pool.map(lambda i: (self.conn if i % 2 else other).access(), range(24)))
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual(self.state["refreshes"], 1)
        self.assertEqual(self.conn.stored()["refresh_token"], "FAKE-ROTATED-1")
        self.assertEqual(self.conn.data.stat().st_mode & 0o777, 0o700)
        for p in self.conn.data.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.conn.data.glob(".pending-*")))

    def test_singleflight_between_independent_processes(self):
        import subprocess
        import sys
        self.seed(time.time() + 30)
        code = "import codex; c=codex.Connection(" + repr(str(self.conn.data)) + "," + repr(self.upstream) + "," + repr(self.upstream + "/backend-api") + "); c.access()"
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent, capture_output=True, timeout=15).returncode, range(6)))
        self.assertEqual(results, [0] * 6)
        self.assertEqual(self.state["refreshes"], 1)
        self.assertEqual(self.conn.stored()["refresh_token"], "FAKE-ROTATED-1")

    def test_invalid_grant_expires_erases_chain_and_no_retries(self):
        self.seed(time.time() - 1); self.state["invalid"] = True
        for _ in range(3):
            with self.assertRaises(codex.Fault) as e:
                self.conn.access()
            self.assertEqual(e.exception.code, "expired")
        self.assertEqual(self.state["refreshes"], 1)
        self.assertEqual(self.conn.stored(), {"status": "expired"})

    def test_backend_401_refresh_once_does_not_destroy_valid_chain(self):
        self.seed(); self.state["mode"] = "unauthorized"
        with self.assertRaises(codex.Fault) as e: self.conn.models()
        self.assertEqual(e.exception.status, 401)
        self.assertEqual(self.state["refreshes"], 1)
        self.assertEqual(self.conn.stored()["status"], "connected")
        self.assertEqual(self.conn.stored()["refresh_token"], "FAKE-ROTATED-1")
        self.state["mode"] = "ok"
        self.assertEqual(self.conn.models()[0]["id"], "gpt-5.6-sol")

    def test_refresh_chain_failure_codes_expire_once(self):
        for code in ("refresh_token_expired", "refresh_token_reused", "REFRESH_TOKEN_INVALIDATED"):
            self.state["invalid"] = True; self.state["invalid_code"] = code
            self.seed(time.time() - 1)
            count = self.state["refreshes"]
            for _ in range(2):
                with self.assertRaises(codex.Fault) as e: self.conn.access()
                self.assertEqual(e.exception.code, "expired")
            self.assertEqual(self.state["refreshes"], count + 1)
            self.assertEqual(self.conn.stored(), {"status": "expired"})

    def test_rotated_tokens_survive_transient_disk_failure_without_second_refresh(self):
        self.seed(time.time() - 1)
        atomic = self.conn.atomic
        def disk(name, obj):
            if name == "connection.json" and obj.get("status") == "connected":
                raise OSError("disk full, secret text must not escape")
            atomic(name, obj)
        with patch.object(self.conn, "atomic", side_effect=disk):
            with self.assertRaises(codex.Fault) as e: self.conn.access()
            self.assertNotIn("secret text", str(e.exception))
        self.assertEqual(self.conn.stored()["status"], "refreshing")
        other = codex.Connection(self.conn.data, self.upstream, self.upstream + "/backend-api")
        with self.assertRaises(codex.Fault): other.access()
        self.assertEqual(self.state["refreshes"], 1)
        self.assertTrue(self.conn.access())
        self.assertEqual(self.conn.stored()["refresh_token"], "FAKE-ROTATED-1")
        self.assertEqual(self.state["refreshes"], 1)

    def test_ambiguous_refresh_does_not_replay_after_restart(self):
        self.seed(time.time() - 1)
        with patch.object(self.conn, "json_request", side_effect=codex.Fault("unreachable", "Unavailable", 503)) as req:
            with self.assertRaises(codex.Fault): self.conn.access()
            self.assertEqual(req.call_count, 1)
        other = codex.Connection(self.conn.data, self.upstream, self.upstream + "/backend-api")
        with self.assertRaises(codex.Fault): other.access()
        self.assertEqual(self.state["refreshes"], 0)
        self.assertEqual(server.account_row()["status"], "expired")

    def test_pending_login_network_does_not_block_existing_access(self):
        self.seed()
        request = self.conn.json_request
        entered, release = threading.Event(), threading.Event()
        def waiting(url, *args, **kw):
            if url.endswith("/usercode"):
                entered.set(); release.wait(3)
            return request(url, *args, **kw)
        with patch.object(self.conn, "json_request", side_effect=waiting):
            with ThreadPoolExecutor(max_workers=2) as pool:
                login = pool.submit(self.conn.connect)
                self.assertTrue(entered.wait(1))
                try:
                    self.assertTrue(pool.submit(self.conn.access).result(timeout=.4))
                finally:
                    release.set()
                self.assertTrue(login.result()["ok"])

    def test_cancel_wrong_login_and_access_only_no_refresh(self):
        d = self.conn.connect()
        with self.assertRaises(codex.Fault): self.conn.poll("wrong")
        self.conn.cancel()
        with self.assertRaises(codex.Fault): self.conn.poll(d["login_id"])
        self.seed()
        s = self.conn.stored(); s.pop("refresh_token")
        self.conn.atomic("connection.json", s)
        self.assertEqual(self.conn.access(), s["access_token"])
        s["expires_at"] = 1; self.conn.atomic("connection.json", s)
        with self.assertRaises(codex.Fault): self.conn.access()
        self.assertEqual(self.state["refreshes"], 0)

    def test_stopped_copy_migration_preserves_key_rotates_on_new_box(self):
        self.login()
        self.seed(time.time() - 1)
        target = Path(self.tmp.name) / "new-box" / "app-data"
        shutil.copytree(self.conn.data, target)
        # Old instance stopped: no further call against source store.
        new = codex.Connection(target, self.upstream, self.upstream + "/backend-api")
        self.assertEqual(new.gateway_token(), self.conn.gateway_token())
        self.assertTrue(new.access())
        self.assertEqual(new.stored()["refresh_token"], "FAKE-ROTATED-1")
        self.assertEqual(self.state["refreshes"], 1)

    def test_csrf_page_auth_gateway_ignores_page_password(self):
        self.assertEqual(self.call("/api/chatgpt/connect", {})[0], 403)
        self.assertEqual(self.call("/api/chatgpt/connect", {}, {"Origin": "https://evil.example", "X-Wizard-Request": "1"})[0], 403)
        with patch.object(server, "PASSWORD", "owner-pass"):
            self.assertEqual(self.call("/api/accounts")[0], 401)
            self.assertEqual(self.call("/v1/models", headers={"Authorization": "Basic " + base64.b64encode(b"owner:owner-pass").decode()})[0], 401)


class GatewayTests(AppCase):
    def test_gateway_header_auth_path_refusals_and_secret_error_sanitization(self):
        self.seed()
        self.assertEqual(self.call("/v1/models")[0], 401)
        self.assertEqual(self.call("/v1/models", headers={"Authorization": "Bearer wrong"})[0], 401)
        for k in ("Origin", "Referer", "Cookie", "Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "CF-Ray", "Sec-Fetch-Site", "Via", "Tailscale-User-Login"):
            self.assertEqual(self.call("/v1/models", headers={**self.auth(), k: "browser"})[0], 403, k)
        # Node's fetch (the Agent Service usage reader) always sends Sec-Fetch-Mode: cors and nothing else.
        self.assertEqual(self.call("/v1/models", headers={**self.auth(), "Sec-Fetch-Mode": "cors"})[0], 200)
        self.assertEqual(self.call("/v1/models", headers={**self.auth(), "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.call("/v1/evil", {}, self.auth())[0], 404)
        self.state["mode"] = "error"
        status, raw, hdr = self.call("/v1/responses", {"model": "gpt-5.6-sol", "input": []}, self.auth())
        self.assertEqual(status, 400)
        self.assertNotIn(self.state["refresh"], raw.decode())
        self.assertNotIn("eyJ", raw.decode())
        self.assertFalse(any(k.lower() == "set-cookie" for k in hdr))

    def test_models_usage_compaction_and_real_token_injection(self):
        self.seed()
        status, raw, _ = self.call("/v1/models", headers=self.auth())
        self.assertEqual((status, json.loads(raw)["data"][0]["id"]), (200, "gpt-5.6-sol"))
        self.assertEqual(json.loads(raw)["models"][0]["slug"], "gpt-5.6-sol")  # Codex shape for stock Hermes
        self.assertEqual(self.call("/v1/api/codex/usage", headers=self.auth())[0], 200)
        self.assertEqual(self.call("/v1/api/codex/rate-limit-reset-credits", headers=self.auth())[0], 200)
        status, raw, _ = self.call("/v1/responses/compact", {"model": "gpt-5.6-sol", "input": []}, self.auth())
        self.assertEqual((status, json.loads(raw)["object"]), (200, "response.compaction"))
        h = {k.lower(): v for k, v in self.state["requests"][-1][2].items()}
        self.assertEqual(h["authorization"], "Bearer " + self.conn.stored()["access_token"])
        self.assertEqual(h["chatgpt-account-id"], "FAKE-ACCOUNT-PRIVATE")
        self.assertEqual(h["originator"], "hermes-agent")
        self.assertNotIn(self.conn.gateway_token(), str(h))

    def test_streaming_flush_normalization_and_safe_headers(self):
        self.seed(); self.state["mode"] = "slow"
        body = {"model": "gpt-5.6-sol", "input": [], "max_tokens": 100, "temperature": .2}
        req = Request(self.base + "/v1/responses", data=json.dumps(body).encode(), headers={**self.auth(), "Content-Type": "application/json"})
        with urlopen(req, timeout=3) as r:
            raw = b""
            while b"response.output_text.delta" not in raw:
                raw += r.readline()
            self.assertFalse(self.state["continue"].is_set())
            self.assertEqual(r.headers["x-codex-primary-used-percent"], "25")
            self.assertIsNone(r.headers.get("Set-Cookie"))
            self.state["continue"].set()
            raw += r.read()
            self.assertIn(b"response.completed", raw)
        data = self.state["requests"][-1][3]
        self.assertTrue(data["stream"]); self.assertFalse(data["store"])
        self.assertNotIn("max_tokens", data); self.assertNotIn("temperature", data)


class RegistrationTests(AppCase):
    def dashboard(self):
        self.rows = []
        self.endpoints = []
        self.native_adds = 0
        self.deletes = []
        self.dashboard_writes = []
        def call(method, path, body=None, **kw):
            if method == "GET" and path == "/api/providers/oauth":
                return {"providers": [{"id": "openai-codex", "status": {"logged_in": False, "token_preview": None}}]}
            if method == "GET" and path == "/api/credentials/pool":
                return {"providers": [{"provider": "openai-codex", "entries": list(self.rows)}]}
            if method == "GET" and path == "/api/providers/custom-endpoints":
                return {"endpoints": self.endpoints}
            self.dashboard_writes.append((method, path, body))
            if method == "POST" and path == "/api/providers/custom-endpoints":
                self.assertFalse(body["make_default"])
                self.assertEqual(body["api_key"], self.conn.gateway_token())
                self.endpoints = [{"id": body["id"], "base_url": body["base_url"]}]
            if method == "POST" and path == "/api/credentials/pool":
                self.native_adds += 1
                self.assertEqual(body["api_key"], self.conn.gateway_token())
                self.rows.append({"index": 7, "label": body["label"], "has_refresh": False})
            if method == "DELETE" and path.startswith("/api/credentials/pool/"):
                index = int(path.rsplit("/", 1)[1]); self.deletes.append(index)
                self.rows[:] = [r for r in self.rows if r["index"] != index]
            if method == "DELETE" and path.startswith("/api/providers/custom-endpoints/"):
                self.endpoints = []
            return {"ok": True}
        return call

    def test_reconcile_concurrent_single_registration_and_idempotent_key(self):
        self.seed()
        with patch.object(server, "HERMES_PASSWORD", "fake-dash"), patch.object(server.Hermes, "call", side_effect=self.dashboard()):
            with ThreadPoolExecutor(max_workers=8) as pool:
                results = list(pool.map(lambda _: server.reconcile(), range(16)))
            self.assertEqual(results, ["registered"] * 16)
            self.assertEqual(len(self.rows), 1)
            self.assertEqual(self.native_adds, 1)
            self.assertEqual(self.deletes, [])

    def test_legacy_singleton_conflict_before_any_dashboard_write(self):
        self.seed()
        call = self.dashboard()
        def legacy(method, path, body=None, **kw):
            if path == "/api/providers/oauth":
                return {"providers": [{"id": "openai-codex", "status": {"logged_in": True, "token_preview": "last-five"}}]}
            return call(method, path, body, **kw)
        with patch.object(server, "HERMES_PASSWORD", "fake-dash"), patch.object(server.Hermes, "call", side_effect=legacy):
            self.assertTrue(server.register([{"id": "gpt-5.6-sol"}]).startswith("conflict"))
            self.assertEqual(self.dashboard_writes, [])

    def test_conflict_and_unregister_target_index_preserve_other_login(self):
        self.seed()
        call = self.dashboard()
        self.rows = [{"index": 7, "label": server.ENDPOINT_ID}, {"index": 2, "label": "owner-other-account"}]
        with patch.object(server, "HERMES_PASSWORD", "fake-dash"), patch.object(server.Hermes, "call", side_effect=call):
            self.assertTrue(server.register([{"id": "gpt-5.6-sol"}]).startswith("conflict"))
            self.assertEqual(self.dashboard_writes, [])
            server.unregister()
            self.assertEqual(self.deletes, [7])
            self.assertEqual(self.rows, [{"index": 2, "label": "owner-other-account"}])

    def test_failed_key_replacement_does_not_cache_success_fingerprint(self):
        self.seed()
        call = self.dashboard()
        self.rows = [{"index": 7, "label": server.ENDPOINT_ID, "has_refresh": False}]
        self.conn.atomic("registration.json", {"status": "registered", "key_sha256": "old-key", "base_url": server.AGENT_BASE})
        fail = [True]
        def transient(method, path, body=None, **kw):
            if method == "DELETE" and path.startswith("/api/credentials/pool/") and fail[0]:
                fail[0] = False
                raise server.HermesError("transient")
            return call(method, path, body, **kw)
        with patch.object(server, "HERMES_PASSWORD", "fake-dash"), patch.object(server.Hermes, "call", side_effect=transient):
            self.assertEqual(server.reconcile(), "unavailable")
            self.assertNotIn("key_sha256", json.loads((self.conn.data / "registration.json").read_text()))
            self.assertEqual(server.reconcile(), "registered")
            self.assertEqual(self.native_adds, 1)

    def test_absent_endpoint_cleanup_is_idempotent(self):
        self.seed()
        with patch.object(server, "HERMES_PASSWORD", "fake-dash"), patch.object(server.Hermes, "call", side_effect=self.dashboard()):
            server.unregister(); server.unregister()
        self.assertEqual(server.registration(), "not_registered")
        self.assertFalse(any(method == "DELETE" and path.startswith("/api/providers/custom-endpoints/") for method, path, _ in self.dashboard_writes))

    def test_failed_cleanup_and_registration_status_disk_error_are_honest(self):
        self.seed()
        with patch.object(server, "HERMES_PASSWORD", "fake-dash"), patch.object(server.Hermes, "call", side_effect=server.HermesError("unavailable")):
            server.unregister()
            self.assertEqual(server.registration(), "unregister_failed")
        atomic = self.conn.atomic
        def failing(name, obj):
            if name == "registration.json": raise OSError("disk full")
            return atomic(name, obj)
        with patch.object(server, "register", return_value="registered"), patch.object(self.conn, "atomic", side_effect=failing):
            self.assertEqual(server.reconcile(), "registered")
        self.assertEqual(self.conn.stored()["status"], "connected")


if __name__ == "__main__":
    unittest.main(verbosity=2)
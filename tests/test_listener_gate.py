"""Browser boundary on this app's listener (wizard-os spec/browser-trust-shell-intents): with WIZARD_BROWSER_BOUNDARY=1
the accounts routes answer only a box-local caller holding this installation's app credential. Frames/pages (Origin,
Sec-Fetch-*, Cookie), network owner identity (Tailscale Serve headers) and preflights are refused by the real MCP front
process; /api/connection and /api/infer keep their own grants; the page itself still loads; flag off is unchanged.
Synthetic stub engine only; no provider, no real account."""
import http.client, http.server, json, os, secrets, socket, subprocess, sys, tempfile, threading, time, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARDED = [g for g in (ROOT / "runtipi" / "docker-compose.yml").read_text().split("WIZARD_GUARDED_PATHS: ", 1)[1].split("\n", 1)[0].split(",")]
SEEN = []


class Engine(http.server.BaseHTTPRequestHandler):
    def _any(self):
        SEEN.append({"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)
        body = json.dumps({"ok": True, "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_OPTIONS = _any

    def log_message(self, format, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Front:
    def __init__(self, tmp, engine_port, **env):
        self.port = free_port()
        f = Path(tmp) / "front.py"
        f.write_text((ROOT / "mcp" / "front.py").read_text() + "\n\n" + (ROOT / "mcp" / "tools.py").read_text()
                     + '\n\nif __name__ == "__main__":\n    asyncio.run(main())\n')
        e = {k: v for k, v in os.environ.items() if not k.startswith("WIZARD_")}
        e.update(WIZARD_APP="synthetic", FRONT_PORT=str(self.port), UPSTREAM_HOST="127.0.0.1", UPSTREAM_PORT=str(engine_port),
                 PYTHONDONTWRITEBYTECODE="1", **env)
        self.proc = subprocess.Popen([sys.executable, str(f)], env=e, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(100):
            if self.proc.poll() is not None:
                break
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.05)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        return self.proc.communicate(timeout=10)[0].decode()

    def req(self, method, path, headers=(), body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", "box.example.ts.net")
        for k, v in headers:
            c.putheader(k, v)
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data


class ListenerGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Engine)
        threading.Thread(target=cls.engine.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp()
        cls.cred = secrets.token_hex(32)
        cls.on = Front(cls.tmp, cls.engine.server_address[1], WIZARD_BROWSER_BOUNDARY="1", WIZARD_APP_CREDENTIAL=cls.cred,
                       WIZARD_GUARDED_PATHS=",".join(GUARDED), WIZARD_PAGE_PASSWORD="synthetic-page-password")
        cls.off = Front(tempfile.mkdtemp(), cls.engine.server_address[1], WIZARD_GUARDED_PATHS=",".join(GUARDED))

    @classmethod
    def tearDownClass(cls):
        cls.on.stop(); cls.off.stop(); cls.engine.shutdown()

    def setUp(self):
        SEEN.clear()

    def test_guarded_paths_cover_the_accounts_routes(self):
        self.assertIn("/api/accounts/", GUARDED)
        self.assertIn("/api/operations/", GUARDED)
        self.assertNotIn("/api/connection/", GUARDED)   # Agent Service usage reader keeps X-Wizard-Grant
        self.assertNotIn("/api/infer/", GUARDED)        # connection router keeps its own grant

    def test_adapter_with_credential_reaches_engine_without_it(self):
        for path in ("/api/accounts", "/api/operations", "/api/operations/" + "a" * 32):
            status, _ = self.on.req("GET" if path != "/api/operations" else "POST", path,
                                    [("X-Wizard-App-Credential", self.cred), ("Content-Type", "application/json"),
                                     ("Authorization", "Basic c3RvbGVuOnN0b2xlbg==")], b"{}" if path == "/api/operations" else None)
            self.assertEqual(status, 200, path)
        self.assertEqual(len(SEEN), 3)
        for r in SEEN:
            self.assertNotIn("x-wizard-app-credential", r["headers"])        # never passed on
            self.assertEqual(r["headers"]["authorization"], "Basic d2l6YXJkOnN5bnRoZXRpYy1wYWdlLXBhc3N3b3Jk")  # page password, not the request's

    def test_refusals_never_reach_the_engine(self):
        cases = [
            ([], 401), ([("X-Wizard-App-Credential", "0" * 64)], 401),
            ([("X-Wizard-App-Credential", self.cred), ("X-Wizard-App-Credential", self.cred)], 401),
            ([("X-Wizard-App-Credential", self.cred), ("Origin", "null")], 403),
            ([("Origin", "null"), ("Sec-Fetch-Site", "cross-site"), ("Sec-Fetch-Mode", "cors")], 403),
            ([("Sec-Fetch-Site", "same-origin")], 403), ([("Cookie", "x=1")], 403), ([("Referer", "https://box/")], 403),
            ([("Tailscale-User-Login", "owner@example.com")], 403), ([("X-Forwarded-For", "100.64.0.2")], 403),
            ([("Cf-Connecting-Ip", "1.2.3.4"), ("X-Wizard-App-Credential", self.cred)], 403),
        ]
        for path in ("/api/accounts", "/api/operations/" + "b" * 32) + (("/claude-code/api/accounts",) if "/claude-code/api/" in GUARDED else ()):
            for headers, want in cases:
                status, body = self.on.req("GET", path, headers)
                self.assertEqual(status, want, (path, headers))
                self.assertNotIn(b"access-control", body.lower())
        status, _ = self.on.req("POST", "/api/operations", [("Origin", "https://box.example.ts.net"), ("Content-Type", "text/plain")], b"{}")
        self.assertEqual(status, 403)   # simple/form post from a page
        status, _ = self.on.req("OPTIONS", "/api/operations", [("Origin", "null"), ("Access-Control-Request-Method", "POST")])
        self.assertEqual(status, 403)   # preflight
        self.assertEqual(SEEN, [])

    def test_unguarded_routes_unchanged(self):
        for path in ("/", "/app.js", "/api/connection", "/health"):
            self.assertEqual(self.on.req("GET", path, [("Origin", "null"), ("Sec-Fetch-Site", "cross-site")])[0], 200, path)
        self.assertEqual(SEEN[2]["path"], "/api/connection")
        self.assertNotIn("authorization", SEEN[2]["headers"])   # the page password is only added to admitted adapter calls

    def test_flag_off_is_unchanged(self):
        self.assertEqual(self.off.req("GET", "/api/accounts", [("Origin", "null"), ("Tailscale-User-Login", "o@example.com")])[0], 200)
        self.assertEqual(SEEN[0]["headers"].get("tailscale-user-login"), "o@example.com")

    def test_flag_on_without_credential_refuses_to_start(self):
        bad = Front(tempfile.mkdtemp(), self.engine.server_address[1], WIZARD_BROWSER_BOUNDARY="1", WIZARD_GUARDED_PATHS=",".join(GUARDED))
        out = bad.stop()
        self.assertIn("needs WIZARD_APP_CREDENTIAL", out)
        self.assertNotEqual(bad.proc.returncode, 0)


if __name__ == "__main__":
    unittest.main()

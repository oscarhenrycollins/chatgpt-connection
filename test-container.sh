#!/bin/sh
# Build the image, then prove inside it (read-only, no capabilities, uid 1000): the file list, health,
# the page, the card, the wizard.ai-connection/2 answer with no agent dashboard (error / unreachable,
# usage null), box-only refusals, no credential surface, cross-site refusal, and a clean SIGTERM exit.
# The device-code flow itself is covered by test-chatgpt.py against a stand-in dashboard.
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
TAG=${WIZARD_CHATGPT_IMAGE:-wizard-chatgpt:test}
[ -n "${SKIP_BUILD:-}" ] || docker build -t "$TAG" "$ROOT"
docker run --rm --read-only --cap-drop ALL --security-opt no-new-privileges --memory 128m --pids-limit 64 \
  --network none -e WIZARD_CHATGPT_PORT=8801 --entrypoint python3 "$TAG" -c '
import json, os, pathlib, subprocess, sys, time, urllib.request, urllib.error
assert os.getuid() == 1000 and os.getgid() == 1000, "must run as 1000:1000"
files = sorted(str(p.relative_to("/app")) for p in pathlib.Path("/app").rglob("*") if p.is_file())
assert files == ["LICENSE", "SKILL.md", "inference.py", "server.py", "static/app.css", "static/app.js", "static/index.html"], files
p = subprocess.Popen([sys.executable, "/app/server.py"])
B = "http://127.0.0.1:8801"
def req(path, body=None, headers=None):
    h = {"Content-Type": "application/json"} if body is not None else {}
    h.update(headers or {})
    r = urllib.request.Request(B + path, data=None if body is None else json.dumps(body).encode(), headers=h)
    try:
        with urllib.request.urlopen(r, timeout=30) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
t0 = time.monotonic()
while time.monotonic() - t0 < 10:
    try:
        code, body = req("/health"); break
    except OSError: time.sleep(0.1)
assert json.loads(body) == {"status": "ok", "version": "0.2.0"}, body
code, body = req("/"); assert code == 200 and b"<title>ChatGPT</title>" in body
h = urllib.request.urlopen(B + "/", timeout=30).headers
assert h["Content-Security-Policy"].split("frame-ancestors ")[1].startswith(chr(39) + "self" + chr(39) + ";") and h["X-Frame-Options"] == "SAMEORIGIN", dict(h)
for f in ("/app.js", "/app.css", "/SKILL.md"): assert req(f)[0] == 200, f
cardj = json.loads(req("/.well-known/mcp/server-cards.json")[1])
assert cardj["name"] == "wizard-chatgpt" and cardj["x-wizard"]["provides"] == ["ai-connection", "chatgpt-subscription"], cardj
assert cardj["x-wizard"]["contract"] == "wizard.ai-connection/2" and cardj["x-wizard"]["agent_dashboard"] == "missing", cardj
code, body = req("/api/connection"); conn = json.loads(body)
assert code == 200 and list(conn) == ["contract", "provider", "name", "status", "plan", "account", "usage", "error", "models"], conn
assert conn["contract"] == "wizard.ai-connection/2" and conn["provider"] == "chatgpt" and conn["status"] == "error", conn
assert conn["usage"] is None and conn["error"]["code"] == "unreachable" and "Hermes Agent" in conn["error"]["message"], conn
for h in ({"Tailscale-User-Login": "x@y"}, {"X-Forwarded-For": "1.2.3.4"}, {"Forwarded": "for=1.2.3.4"},
          {"X-Real-IP": "1.2.3.4"}, {"CF-Connecting-IP": "1.2.3.4"}, {"cf-ray": "abc"}):
    code, body = req("/api/connection", None, h)
    assert code == 403 and b"box-only" in body and b"status" not in body, (h, code, body)
assert req("/", None, {"Tailscale-User-Login": "x@y"})[0] == 200
for path in ("/auth.json", "/server.py", "/static/../server.py", "/api/chatgpt/token", "/.hermes/auth.json"):
    assert req(path)[0] == 404, path
assert req("/api/chatgpt/use", {}, {"Origin": "http://evil.example"})[0] == 403
code, body = req("/api/chatgpt/connect", {}); assert code == 502 and b"Hermes Agent" in body, (code, body)
p.terminate(); rc = p.wait(10)
print(json.dumps({"uid": os.getuid(), "status": conn["status"], "error": conn["error"]["code"], "sigterm_exit": rc}))
assert rc == 0, rc'
docker image inspect "$TAG" --format 'image size bytes: {{.Size}}'

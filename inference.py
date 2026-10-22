"""Isolated subscription inference and router ingress (stdlib only)."""
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import threading
import time

MAX_BODY = 256 * 1024
CODES = {"signed_out", "expired", "limit", "unreachable", "provider_error"}
SLOTS = threading.BoundedSemaphore(2)


class Fault(Exception):
    def __init__(self, code="provider_error", message="Provider inference failed.", status=502):
        self.code, self.status = code, status
        super().__init__(message)

    def payload(self):
        return {"type": "error", "code": self.code, "message": str(self)}


def admit(headers):
    """Same grants file shape as the router; no owner/browser credentials accepted."""
    refused = {"origin", "tailscale-user-login", "x-forwarded-for", "forwarded", "x-real-ip"}
    if any(k.lower() in refused or k.lower().startswith("cf-") for k in headers):
        raise Fault(message="Inference is box-only.", status=403)
    path = os.environ.get("WIZARD_GRANTS_FILE")
    grants = {"enforce": False, "incoming": []}
    if path:
        try:
            grants = json.loads(Path(path).read_text())
            if type(grants.get("enforce")) is not bool or not isinstance(grants.get("incoming"), list):
                raise ValueError()
            for row in grants["incoming"]:
                if not isinstance(row, dict) or not isinstance(row.get("from"), str) or not isinstance(row.get("sha256"), str) or len(row["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in row["sha256"]):
                    raise ValueError()
        except (OSError, ValueError, TypeError, AttributeError):
            raise Fault(message="Grants configuration is unavailable or invalid.", status=503) from None
    token = headers.get("X-Wizard-Grant", "")
    digest = hashlib.sha256(token.encode()).hexdigest()
    for row in grants["incoming"]:
        if token and hmac.compare_digest(digest, row["sha256"]):
            if row["from"] != "wizard-connection-router":
                raise Fault(message="Only the connection router may infer.", status=403)
            return True
    if grants["enforce"]:
        raise Fault(message="Unknown router grant.", status=403)
    logging.getLogger("inference").warning("Unknown router grant (report-only); caller identity is unverified")
    return False


def body(handler):
    if handler.headers.get("Transfer-Encoding") or handler.headers.get("Content-Encoding"):
        raise Fault(message="Encoded request bodies are unsupported.", status=415)
    if handler.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
        raise Fault(message="JSON only.", status=415)
    try:
        n = int(handler.headers.get("Content-Length", "0"))
        if not 0 < n <= MAX_BODY:
            raise ValueError()
    except ValueError:
        raise Fault("limit", "Invalid request size.", 413) from None
    def pairs(rows):
        out = {}
        for k, v in rows:
            if k in out:
                raise ValueError()
            out[k] = v
        return out
    try:
        data = json.loads(handler.rfile.read(n), object_pairs_hook=pairs)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise Fault(message="Invalid JSON object.", status=400) from None


def validate(data, models):
    allowed = {"model", "system", "messages", "max_tokens", "stream", "abilities"}
    m = next((m for m in models if m["id"] == data.get("model")), None)
    messages = data.get("messages")
    abilities = data.get("abilities", [])
    if (set(data) - allowed or m is None or not isinstance(data.get("system", ""), str)
            or data.get("stream", True) is not True
            or not isinstance(messages, list) or not 1 <= len(messages) <= 128
            or any(not isinstance(v, dict) or set(v) != {"role", "content"}
                   or v["role"] not in ("user", "assistant") or not isinstance(v["content"], str) for v in messages)
            or not isinstance(abilities, list) or any(not isinstance(a, str) or a not in m["abilities"] for a in abilities)
            or type(data.get("max_tokens", 8192)) is not int or not 1 <= data.get("max_tokens", 8192) <= 32768):
        raise Fault(message="Invalid inference contract or unsupported model/ability.", status=400)
    return data


def start(handler):
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()


def emit(handler, event):
    handler.wfile.write(("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n").encode())
    handler.wfile.flush()


from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request

MODELS_PATH = "/api/plugins/wizard-os/models/chatgpt"
INFER_PATH = "/api/plugins/wizard-os/infer/chatgpt"
REQUIRED = "Hermes plugin wizard-os >=0.4.0 required"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def models(hermes):
    payload = hermes.call("GET", MODELS_PATH, timeout=8)
    try:
        if not isinstance(payload, dict):
            raise ValueError()
        rows = payload.get("models")
        if not isinstance(rows, list):
            raise ValueError()
        seen = set()
        for m in rows:
            if (not isinstance(m, dict) or not isinstance(m.get("id"), str) or not m["id"] or m["id"] in seen
                    or not isinstance(m.get("label"), str) or m.get("tier") not in ("fast", "standard", "frontier")
                    or not isinstance(m.get("abilities"), list) or any(a not in ("text", "streaming") for a in m["abilities"])):
                raise ValueError()
            seen.add(m["id"])
        return rows
    except (ValueError, TypeError):
        raise Fault(message="Invalid plugin model catalogue.") from None


def upstream(hermes, url, data):
    if not hermes.configured():
        raise Fault("unreachable", "Hermes dashboard is not configured.", 503)
    with hermes.lock:
        if hermes.opener is None:
            try:
                hermes._login()
            except Exception:
                raise Fault("unreachable", "Hermes dashboard login failed.", 503) from None
        opener = hermes.opener
    # Never forward the caller's grants, cookies, Authorization or identity hints.
    payload = {k: v for k, v in data.items() if k != "abilities"}
    payload["stream"] = True
    req = Request(url + INFER_PATH, data=json.dumps(payload).encode(), method="POST",
                  headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
    try:
        return opener.open(req, timeout=120)
    except HTTPError as e:
        if e.code == 404:
            e.close()
            raise Fault(message=REQUIRED) from None
        status = e.code
        try:
            payload = json.loads(e.read(MAX_BODY + 1))
            code = (payload.get("error") or {}).get("code")
        except (ValueError, TypeError, AttributeError):
            code = None
        finally:
            e.close()
        if status == 401:
            # Do not replay an inference POST. A later request can renew the dashboard session.
            with hermes.lock:
                hermes.opener = None
        code = code if code in CODES else {401: "signed_out", 429: "limit", 503: "unreachable"}.get(status, "provider_error")
        raise Fault(code, "Hermes inference rejected the request.", status if status in (400, 401, 403, 413, 415, 429, 503) else 502) from None


def relay(handler, response, model, lines=None):
    """Preserve delta/done frames; normalize the plugin's nested error for the router."""
    frame = []
    total = size = 0
    while True:
        raw = next(lines) if lines is not None else response.readline(MAX_BODY + 1)
        if raw is None:
            handler.wfile.write(b": heartbeat\n\n")
            handler.wfile.flush()
            continue
        if not raw:
            raise Fault(message="Plugin ended without an effective-model done event.")
        total += len(raw)
        size += len(raw)
        if size > MAX_BODY or total > 4 * 1024 * 1024:
            raise Fault("limit", "Plugin stream exceeded its limit.")
        frame.append(raw)
        if raw.strip():
            continue
        frame_lines = b"".join(frame).decode("utf-8").splitlines()
        kind = next((v[6:].strip() for v in frame_lines if v.startswith("event:")), "")
        text = "\n".join(v[5:].lstrip() for v in frame_lines if v.startswith("data:"))
        if text:
            obj = json.loads(text)
            if not isinstance(obj, dict) or obj.get("type", kind) != kind:
                raise Fault(message="Malformed plugin event.")
            if kind == "error":
                code = (obj.get("error") or {}).get("code", obj.get("code"))
                raise Fault(code if code in CODES else "provider_error")
            if kind not in ("delta", "done"):
                raise Fault(message="Unexpected plugin event.")
            if kind == "delta" and not isinstance(obj.get("text"), str):
                raise Fault(message="Invalid plugin text.")
            if kind == "done" and (obj.get("ok") is not True or obj.get("model") != model or not isinstance(obj.get("usage"), dict)):
                raise Fault(message="Plugin effective model or completion is invalid.")
        handler.wfile.write(b"".join(frame))
        handler.wfile.flush()
        frame, size = [], 0
        if text and kind == "done":
            return


STREAM_TIMEOUT_S = 120


def stream_lines(response):
    # A bounded reader queue keeps heartbeats/cancellation independent of provider silence.
    import queue
    import socket
    pending = queue.Queue(maxsize=2)
    stopped = threading.Event()
    def offer(value):
        while not stopped.is_set():
            try:
                pending.put(value, timeout=.1)
                return
            except queue.Full:
                pass
    def read():
        try:
            while not stopped.is_set():
                line = response.readline(MAX_BODY + 1)
                offer(line)
                if not line:
                    return
        except Exception:
            offer(Fault("unreachable", "Plugin stream failed."))
    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    deadline = time.monotonic() + STREAM_TIMEOUT_S
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Fault("unreachable", "Plugin stream timed out.")
            try:
                value = pending.get(timeout=min(2, remaining))
            except queue.Empty:
                yield None
                continue
            if isinstance(value, Exception):
                raise value
            yield value
            if not value:
                return
    finally:
        stopped.set()
        # urllib HTTPResponse.close alone can wait for the reader's buffered-I/O lock.
        # Shut down its socket first so a disconnected caller never leaves a live POST.
        try:
            response.fp.raw._sock.shutdown(socket.SHUT_RDWR)
        except (OSError, AttributeError):
            pass
        worker.join(timeout=2)
        response.close()


def handle(handler, hermes, url):
    started = acquired = False
    try:
        admit(handler.headers)
        handler.connection.settimeout(10)
        data = body(handler)
        try:
            catalogue = models(hermes)
        except Exception as e:
            # Only typed, private dashboard errors; no exception text crosses this route.
            if str(e) == REQUIRED:
                raise Fault(message=REQUIRED) from None
            raise Fault(getattr(e, "code", "provider_error"), "Plugin models are unavailable.") from None
        validate(data, catalogue)
        if not SLOTS.acquire(blocking=False):
            raise Fault("limit", "Inference capacity busy.", 429)
        acquired = True
        with upstream(hermes, url, data) as response:
            if response.headers.get_content_type() != "text/event-stream":
                raise Fault(message="Plugin did not return SSE.")
            start(handler)
            started = True
            lines = stream_lines(response)
            try:
                relay(handler, response, data["model"], lines)
            finally:
                lines.close()
    except Fault as e:
        if started:
            emit(handler, e.payload())
        else:
            handler.json(e.status, e.payload())
    except (BrokenPipeError, ConnectionResetError):
        pass
    except OSError:
        e = Fault("unreachable", "Hermes connection failed.", 503)
        if started:
            emit(handler, e.payload())
        else:
            handler.json(e.status, e.payload())
    except (ValueError, TypeError, AttributeError):
        e = Fault(message="Malformed plugin stream.")
        if started:
            emit(handler, e.payload())
        else:
            handler.json(e.status, e.payload())
    finally:
        if acquired:
            SLOTS.release()

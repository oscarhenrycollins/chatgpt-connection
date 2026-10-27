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
    refused = {"origin", "referer", "cookie", "tailscale-user-login", "forwarded", "x-real-ip", "via"}
    # `Sec-Fetch-Mode` alone is Node's fetch on the box; browsers also send Sec-Fetch-Site/Dest.
    if any(k.lower() in refused or (k.lower().startswith(("cf-", "x-forwarded-", "sec-fetch-")) and k.lower() != "sec-fetch-mode") for k in headers):
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


def body(handler, max_body=MAX_BODY):
    if handler.headers.get("Transfer-Encoding") or handler.headers.get("Content-Encoding"):
        raise Fault(message="Encoded request bodies are unsupported.", status=415)
    if handler.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
        raise Fault(message="JSON only.", status=415)
    try:
        n = int(handler.headers.get("Content-Length", "0"))
        if not 0 < n <= max_body:
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
        if len(handler.headers.get_all("Content-Length", [])) != 1:
            raise ValueError()
        data = json.loads(handler.rfile.read(n), object_pairs_hook=pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
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
            offer(Fault("unreachable", "ChatGPT stream failed."))
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


def handle_owned(handler, connection):
    """Connection Router /2 ingress, translated directly to the app-owned Codex stream."""
    from codex import Fault as CodexFault
    started = acquired = False
    try:
        admit(handler.headers)
        handler.connection.settimeout(10)
        data = body(handler)
        validate(data, connection.models())
        if not SLOTS.acquire(blocking=False):
            raise Fault("limit", "Inference capacity busy.", 429)
        acquired = True
        payload = {"model": data["model"], "instructions": data.get("system") or "You are a helpful assistant.",
                   "input": [{"role": m["role"], "content": m["content"]} for m in data["messages"]],
                   "store": False, "stream": True}
        with connection.open("POST", "/codex/responses", payload) as response:
            start(handler)
            started = True
            lines = stream_lines(response)
            total = 0
            try:
                for raw in lines:
                    if raw is None:
                        handler.wfile.write(b": heartbeat\n\n")
                        handler.wfile.flush()
                        continue
                    if not raw:
                        raise Fault(message="ChatGPT stream ended without completion.")
                    total += len(raw)
                    if len(raw) > MAX_BODY or total > 4 * 1024 * 1024:
                        raise Fault("limit", "ChatGPT stream exceeded its limit.")
                    if not raw.startswith(b"data:"):
                        continue
                    event = json.loads(raw[5:])
                    kind = event.get("type")
                    if kind == "response.output_text.delta":
                        emit(handler, {"type": "delta", "text": event["delta"]})
                    elif kind == "response.completed":
                        effective = event.get("response", {})
                        if effective.get("model") != data["model"]:
                            raise Fault(message="ChatGPT returned a different effective model.")
                        emit(handler, {"type": "done", "ok": True, "model": data["model"], "usage": effective.get("usage") or {}})
                        return
                    elif kind in ("error", "response.failed", "response.incomplete"):
                        raise Fault(message="ChatGPT did not complete the request.")
            finally:
                lines.close()
    except (Fault, CodexFault) as e:
        event = {"type": "error", "code": e.code, "message": str(e)}
        emit(handler, event) if started else handler.json(e.status, event)
    except (BrokenPipeError, ConnectionResetError):
        pass
    except (OSError, ValueError, TypeError, KeyError):
        event = {"type": "error", "code": "provider_error", "message": "ChatGPT inference unavailable."}
        emit(handler, event) if started else handler.json(502, event)
    finally:
        if acquired:
            SLOTS.release()
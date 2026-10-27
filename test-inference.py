"""Connection Router ingress against the app's fake Codex backend, not Hermes."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

import inference
import server
spec = importlib.util.spec_from_file_location("fixtures", Path(__file__).with_name("test-chatgpt.py"))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class InferenceTests(fixtures.AppCase):
    def setUp(self):
        super().setUp()
        self.seed()
        self.grants = Path(self.tmp.name) / "grants.json"
        self.grants.write_text(json.dumps({"enforce": True, "incoming": [{"from": "wizard-connection-router", "sha256": hashlib.sha256(b"router-only").hexdigest()}]}))
        os.environ["WIZARD_GRANTS_FILE"] = str(self.grants)

    def infer(self, changes=None, headers=None):
        body = {"model": "gpt-5.6-sol", "system": "Explicit only", "messages": [{"role": "user", "content": "hello"}], "stream": True, "abilities": ["text"], "max_tokens": 128}
        body.update(changes or {})
        return self.call("/api/infer", body, {"X-Wizard-Grant": "router-only", **(headers or {})})

    def test_direct_owned_token_passthrough_no_dashboard(self):
        with patch.object(server.Hermes, "call", side_effect=AssertionError("No Hermes data-plane calls")):
            status, raw, _ = self.infer()
        self.assertEqual(status, 200)
        self.assertIn(b"event: delta", raw)
        self.assertIn(b'"text": "hello"', raw)
        self.assertIn(b"event: done", raw)
        data = self.state["requests"][-1][3]
        self.assertEqual(data["instructions"], "Explicit only")
        self.assertNotIn("abilities", data)
        hdrs = {k.lower(): v for k,v in self.state["requests"][-1][2].items()}
        self.assertNotIn("x-wizard-grant", hdrs)
        self.assertEqual(hdrs["authorization"], "Bearer " + self.conn.stored()["access_token"])

    def test_grants_proxy_refusal_and_page_password(self):
        for h in ({"X-Wizard-Grant": "wrong"}, {"Origin": "http://browser"}, {"X-Forwarded-Host": "browser"}, {"Sec-Fetch-Site": "same-origin"}):
            self.assertEqual(self.infer(headers=h)[0], 403)
        self.grants.write_text("broken")
        self.assertEqual(self.infer()[0], 503)

    def test_schema_allowlist(self):
        for d in ({"model": "unknown"}, {"system": {}}, {"stream": False}, {"tools": []}, {"url": "http://evil"}, {"abilities": ["vision"]}, {"max_tokens": 0}, {"messages": []}, {"messages": [{"role": "system", "content": "no"}]}):
            self.assertEqual(self.infer(d)[0], 400)

    def test_stream_missing_model_mismatch_and_provider_failure(self):
        for mode in ("missing", "mismatch"):
            self.state["mode"] = mode
            status, raw, _ = self.infer()
            self.assertEqual(status, 200)
            self.assertIn(b"event: error", raw)
            self.assertNotIn(b"event: done", raw)
        self.state["mode"] = "error"
        self.assertEqual(self.infer()[0], 400)

    def test_disconnected_and_capacity(self):
        inference.SLOTS.acquire(); inference.SLOTS.acquire()
        try:
            self.assertEqual(self.infer()[0], 429)
        finally:
            inference.SLOTS.release(); inference.SLOTS.release()
        self.conn.disconnect()
        status, raw, _ = self.infer()
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(raw)["code"], "signed_out")


if __name__ == "__main__":
    unittest.main(verbosity=2)
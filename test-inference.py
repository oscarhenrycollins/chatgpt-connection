"""HTTP integration with a fake authenticated Hermes plugin; never real ChatGPT."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inference
import server
spec = importlib.util.spec_from_file_location('legacy', Path(__file__).with_name('test-chatgpt.py'))
legacy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy)


class Plugin(legacy.Stub):
    mode = 'ok'
    bodies = []
    headers_seen = []

    def route(self, method):
        if self.path == inference.MODELS_PATH and self.mode == 'no-models':
            return self.reply(404, {})
        if self.path != inference.INFER_PATH:
            return super().route(method)
        if not self.authed():
            return self.reply(401, {})
        body = self.body()
        self.bodies.append(body)
        self.headers_seen.append(dict(self.headers))
        if self.mode == 'absent':
            return self.reply(404, {'secret': 'not-returned'})
        if self.mode.startswith('http:'):
            _, status, code = self.mode.split(':')
            return self.reply(int(status), {'error': {'code': code}, 'secret': 'not-returned'})
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        self.wfile.write(b'event: delta\ndata: {"text":"hello"}\n\n')
        self.wfile.flush()
        if self.mode == 'stall':
            self.connection.settimeout(3)
            try:
                self.connection.recv(1)
            except OSError:
                pass
            return
        if self.mode == 'missing':
            return
        if self.mode == 'error':
            self.wfile.write(b'event: error\ndata: {"ok":false,"error":{"code":"limit"}}\n\n')
            return
        if self.mode == 'malformed':
            self.wfile.write(b'event: done\ndata: not-json\n\n')
            return
        done = {'ok': True, 'model': 'wrong' if self.mode == 'mismatch' else body['model'],
                'usage': {'input_tokens': 4, 'output_tokens': 1, 'total_tokens': 5}}
        self.wfile.write(('event: done\ndata: ' + json.dumps(done) + '\n\n').encode())


class InferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        Plugin.mode, Plugin.bodies, Plugin.headers_seen = 'ok', [], []
        legacy.Fake.expire_cookie_once = False
        self.plugin = ThreadingHTTPServer(('127.0.0.1', 0), Plugin)
        self.settings = patch.multiple(server, HERMES_URL=f'http://127.0.0.1:{self.plugin.server_port}',
                                       HERMES_PASSWORD='dash-pass', HERMES_USER='hermes')
        self.settings.start()
        server.Hermes.opener = None
        server.forget()
        self.srv = server.Server(('127.0.0.1', 0), server.Handler)
        self.threads = []
        for srv in (self.plugin, self.srv):
            thread = threading.Thread(target=srv.serve_forever, kwargs={'poll_interval': .01})
            thread.start()
            self.threads.append(thread)
        self.base = f'http://127.0.0.1:{self.srv.server_port}'

    def tearDown(self):
        for srv in (self.srv, self.plugin):
            srv.shutdown()
            srv.server_close()
        for thread in self.threads:
            thread.join()
        self.settings.stop()
        self.env.stop()
        self.tmp.cleanup()

    def call(self, data=None, headers=None):
        if data is None:
            data = {'model': 'gpt-6.1-sol', 'system': 'Explicit only', 'messages': [{'role': 'user', 'content': 'hello'}],
                    'stream': True, 'abilities': ['text'], 'max_tokens': 128}
        req = Request(self.base + '/api/infer', data=json.dumps(data).encode(),
                      headers={'Content-Type': 'application/json', **(headers or {})})
        try:
            res = urlopen(req, timeout=10)
        except HTTPError as e:
            res = e
        with res:
            return res.status, res.read().decode()

    def test_authenticated_payload_and_passthrough(self):
        status, text = self.call(headers={'X-Wizard-Grant': 'caller-secret', 'Authorization': 'not-forwarded'})
        self.assertEqual(status, 200)
        self.assertTrue(text.startswith('event: delta\ndata: {"text":"hello"}\n\n'))
        self.assertIn('event: done', text)
        self.assertEqual(Plugin.bodies, [{'model': 'gpt-6.1-sol', 'system': 'Explicit only', 'messages': [{'role': 'user', 'content': 'hello'}], 'stream': True, 'max_tokens': 128}])
        headers = {k.lower(): v for k, v in Plugin.headers_seen[0].items()}
        self.assertIn('hermes_session_at=ok', headers['cookie'])
        self.assertFalse({'authorization', 'x-wizard-grant', 'origin'} & headers.keys())
        self.assertNotIn('caller-secret', text)
        self.assertNotIn('dash-pass', text)

    def test_missing_routes(self):
        for mode in ('absent', 'no-models'):
            Plugin.mode = mode
            status, text = self.call()
            self.assertEqual(status, 502)
            self.assertEqual(json.loads(text)['message'], inference.REQUIRED)
            self.assertEqual(json.loads(text)['code'], 'provider_error')

    def test_stream_failures(self):
        for mode in ('missing', 'mismatch', 'malformed', 'error'):
            Plugin.mode = mode
            _, text = self.call()
            self.assertNotIn('event: done', text)
            self.assertIn('event: error', text)
            self.assertIn('limit' if mode == 'error' else 'provider_error', text)

    def test_http_error_codes(self):
        for status, code in [(401, 'expired'), (401, 'signed_out'), (429, 'limit'), (503, 'unreachable'), (502, 'provider_error')]:
            Plugin.mode = f'http:{status}:{code}'
            _, text = self.call()
            self.assertEqual(json.loads(text)['code'], code)
            self.assertNotIn('not-returned', text)
        self.assertEqual(len(Plugin.bodies), 5, 'POSTs must not be replayed')

    def test_schema_allowlist_and_abilities(self):
        data = {'model': 'gpt-6.1-sol', 'messages': [{'role': 'user', 'content': 'hi'}]}
        for update in ({'model': 'unknown'}, {'abilities': ['web_search']}, {'profile': 'other'}, {'stream': False}, {'tools': []}):
            self.assertEqual(self.call({**data, **update})[0], 400)
        self.assertEqual(Plugin.bodies, [])

    def test_grants_and_proxy_refusal(self):
        path = Path(self.tmp.name) / 'grants.json'
        path.write_text(json.dumps({'enforce': True, 'incoming': [{'from': 'wizard-connection-router', 'sha256': hashlib.sha256(b'router-test').hexdigest()}]}))
        os.environ['WIZARD_GRANTS_FILE'] = str(path)
        self.assertEqual(self.call()[0], 403)
        self.assertEqual(self.call(headers={'X-Wizard-App': 'wizard-connection-router'})[0], 403)
        headers = {'X-Wizard-Grant': 'router-test'}
        self.assertEqual(self.call(headers=headers)[0], 200)
        for key in ('Origin', 'Forwarded', 'X-Forwarded-For', 'X-Real-IP', 'Tailscale-User-Login', 'CF-Ray'):
            self.assertEqual(self.call(headers={**headers, key: ''})[0], 403)

    def test_stream_deadline_and_capacity(self):
        Plugin.mode = 'stall'
        with patch.object(inference, 'STREAM_TIMEOUT_S', .05):
            _, text = self.call()
        self.assertIn('unreachable', text)
        self.assertNotIn('event: done', text)
        with patch.object(inference, 'SLOTS', threading.BoundedSemaphore(0)):
            self.assertEqual(self.call()[0], 429)
        Plugin.mode = 'ok'
        self.assertIn('event: done', self.call()[1])

    def test_page_password_not_bypassed_by_unknown_grant(self):
        with patch.object(server, 'PASSWORD', 'owner-page-password'):
            request = Request(self.base + '/api/connection', headers={'X-Wizard-Grant': 'unknown'})
            with self.assertRaises(HTTPError) as err:
                urlopen(request)
            self.assertEqual(err.exception.code, 401)
            err.exception.close()

    def test_dashboard_redirect_refused(self):
        self.assertIsNone(inference.NoRedirect().redirect_request(None, None, 302, '', {}, 'http://elsewhere'))


if __name__ == '__main__':
    unittest.main()

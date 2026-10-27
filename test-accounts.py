"""App-owned inventory protocol: offline, no provider accounts or owner credentials."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from accounts import Accounts, Refused


class Adapter:
    def __init__(self, root): self.root, self.states, self.cancelled = root, {}, []
    def atomic(self, name, data):
        tmp = self.root / '.tmp'
        tmp.write_text(json.dumps(data)); tmp.chmod(0o600); tmp.replace(self.root / name)
    def has_legacy(self): return False
    def status(self, ident): return self.states.get(ident, {'status': 'not_connected'})
    def integration(self): return {'connected': False, 'message': "Can't reach your agent."}
    def changed(self): pass
    def connect(self, ident, lease=None): return {'url': 'https://auth.openai.com/codex/device', 'code': 'FAKE-ONLY', 'login_id': ident}
    def poll(self, ident, login_id):
        self.states[ident] = {'status': 'connected', 'account': ident + '@example.invalid'}
        return {'status': 'approved'}
    def submit(self, ident, code):
        self.states[ident] = {'status': 'connected'}
        return {'ok': True}
    def cancel(self, ident): self.cancelled.append(ident)
    def disconnect(self, ident): self.states.pop(ident, None)


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'app-data'
        self.adapter = Adapter(self.root)
        self.pool = Accounts(self.root, 'chatgpt', self.adapter)
    def tearDown(self): self.pool.close(); self.tmp.cleanup()
    def wait(self, job, terminal=False):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.pool.poll(job['id'])
            if result['status'] != 'running' or (not terminal and result['url']): return result
            time.sleep(.005)
        self.fail('operation timed out')
    def add(self, label):
        job = self.pool.start({'action': 'add', 'confirmed': True, 'label': label})
        return self.wait(job, True)
    def test_james_login_belongs_to_connection_app_any_number_accounts_one_page(self):
        jobs = [self.add('Subscription ' + str(i)) for i in range(3)]
        self.assertEqual([j['status'] for j in jobs], ['succeeded'] * 3)
        out = self.pool.public()
        self.assertEqual((out['owner'], len(out['accounts'])), ('app', 3))
        self.assertTrue(all(r['actions'] == {'reconnect': True, 'remove': True} for r in out['accounts']))
        restarted = Accounts(self.root, 'chatgpt', self.adapter)
        self.assertEqual(len(restarted.rows()), 3)
        restarted.close()
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertNotIn('login_id', json.dumps(out))
        self.assertNotIn('FAKE-ONLY', json.dumps(out))
    def test_missing_agent_is_separate_from_provider_login_no_false_alongside_success(self):
        self.add('One')
        out = self.pool.public()
        self.assertEqual(out['status'], 'connected')
        self.assertFalse(out['integration']['connected'])
        self.assertIn("Can't reach", out['integration']['message'])
    def test_expired_login_has_reconnect_action(self):
        job = self.add('One')
        self.adapter.states[job['target']] = {'status': 'expired'}
        out = self.pool.public()
        self.assertEqual(out['status'], 'expired')
        self.assertTrue(out['accounts'][0]['actions']['reconnect'])
        result = self.pool.start({'action': 'reconnect', 'target': job['target'], 'confirmed': True})
        self.assertEqual(self.wait(result, True)['status'], 'succeeded')
        self.assertEqual(self.pool.public()['status'], 'connected')
    def test_remove_exact_id_does_not_remove_other_subscription(self):
        first, second = self.add('One'), self.add('Two')
        job = self.pool.start({'action': 'remove', 'target': first['target'], 'confirmed': True})
        self.assertEqual(self.wait(job, True)['status'], 'succeeded')
        self.assertEqual([r['id'] for r in self.pool.rows()], [second['target']])
    def test_confirmation_and_exact_target_and_path_validation(self):
        for body in ({'action': 'add'}, {'action': 'remove', 'target': '../other', 'confirmed': True}, {'action': 'add', 'target': 'ignored', 'confirmed': True}, {'action': 'add', 'label': '\n', 'confirmed': True}):
            with self.assertRaises(Refused): self.pool.start(body)
        (self.root/'accounts.json').write_text('[{"id":"../escape", "label":"no"}]')
        with self.assertRaises(Refused): self.pool.rows()
    def test_cross_process_operation_lease_blocks_concurrent_signin(self):
        import fcntl
        self.adapter.poll = lambda *args: {'status': 'pending'}
        job = self.pool.start({'action': 'add', 'confirmed': True})
        self.wait(job)
        other = Accounts(self.root, 'chatgpt', self.adapter)
        with self.assertRaises(BlockingIOError): other.start({'action': 'add', 'confirmed': True})
        self.pool.act({'action': 'cancel', 'operation': job['id']})
        self.assertIn(job['target'], self.adapter.cancelled)
        self.assertTrue(other.start({'action': 'add', 'confirmed': True}))
        other.close()
    def test_expired_operation_releases_lease_and_does_not_accept_submit(self):
        self.adapter.poll = lambda *args: {'status': 'pending'}
        job = self.pool.start({'action': 'add', 'confirmed': True})
        self.wait(job)
        self.pool.jobs[job['id']]['expiresAt'] = 0
        self.assertEqual(self.pool.poll(job['id'])['status'], 'expired')
        with self.assertRaises(Refused): self.pool.act({'action': 'submit', 'operation': job['id'], 'code': 'fake-only'})


if __name__ == '__main__': unittest.main(verbosity=2)

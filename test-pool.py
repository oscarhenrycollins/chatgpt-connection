"""Real app accounts facade + native gateway; synthetic provider only."""
import importlib.util
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
import codex
import account_owner
import pool_server
import server
spec = importlib.util.spec_from_file_location('owned_fixtures', Path(__file__).with_name('test-chatgpt.py'))
fixtures = importlib.util.module_from_spec(spec); spec.loader.exec_module(fixtures)


class MultiAccountTests(fixtures.AppCase):
    def setUp(self):
        super().setUp()
        self.sctx.__exit__(None, None, None)
        self.pool = account_owner.Pool(self.conn.data, factory=lambda p: codex.Connection(p, self.upstream, self.upstream + '/backend-api'))
        self.conn = self.pool
        server.OWNER = self.pool
        account_owner._INSTANCE = self.pool
        self.srv = server.Server(('127.0.0.1', 0), pool_server.Handler)
        self.sctx = fixtures.serving(self.srv); self.base = self.sctx.__enter__()
    def tearDown(self):
        self.pool.accounts.close(); account_owner._INSTANCE = None
        super().tearDown()
    def operation(self, body):
        code, raw, _ = self.call('/api/operations', body, {'X-Wizard-Account-Action':'1'})
        self.assertEqual(code, 200, raw)
        job = json.loads(raw)
        deadline = time.monotonic()+4
        while time.monotonic()<deadline:
            entry = self.pool.entry(job['target'])
            if entry.login: entry.login['next'] = 0
            _, raw, _ = self.call('/api/operations/'+job['id'])
            result = json.loads(raw)
            if result['status'] != 'running': return result
            time.sleep(.005)
        self.fail('operation did not complete')
    def test_two_app_owned_signins_restart_and_agent_local_bearer(self):
        first = self.operation({'action':'add','confirmed':True,'label':'Work'})
        second = self.operation({'action':'add','confirmed':True,'label':'Home'})
        self.assertEqual((first['status'],second['status']),('succeeded','succeeded'))
        status, raw, _ = self.call('/api/accounts')
        state = json.loads(raw)
        self.assertEqual((status,state['owner'],len(state['accounts'])),(200,'app',2))
        self.assertFalse(state['integration']['connected'])
        # Home must not falsely say an alongside agent is connected merely because local OAuth succeeded.
        status, raw, _ = self.call('/api/connection')
        self.assertEqual(json.loads(raw)['status'],'error')
        # The consumer gets only a box-local bearer, never either OAuth credential.
        for job in (first,second):
            entry = self.pool.entry(job['target'])
            self.assertEqual(entry.data.parent, self.pool.data/'accounts')
            self.assertTrue((entry.data/'connection.json').is_file())
            self.assertNotEqual(entry.stored()['access_token'],self.pool.gateway_token())
        code, raw, _ = self.call('/v1/responses', {'model':'gpt-5.6-sol','input':[]}, self.auth())
        self.assertEqual(code,200)
        self.assertIn(b'response.completed',raw)
        self.assertNotIn(self.pool.gateway_token(),raw.decode())
        restarted = account_owner.Pool(self.pool.data, factory=self.pool.factory)
        self.assertEqual(len(restarted.accounts.rows()),2)
        self.assertEqual(restarted.gateway_token(),self.pool.gateway_token())
        restarted.accounts.close()
    def test_expired_account_reconnect_and_remove_preserves_other_login(self):
        first = self.operation({'action':'add','confirmed':True})
        second = self.operation({'action':'add','confirmed':True})
        one = self.pool.entry(first['target']); one.atomic('connection.json',{'status':'expired'})
        rows = self.pool.accounts.public()['accounts']
        self.assertEqual(rows[0]['status'],'expired')
        self.assertTrue(rows[0]['actions']['reconnect'])
        self.assertEqual(self.call('/v1/models',headers=self.auth())[0],200)
        self.assertEqual(self.operation({'action':'reconnect','confirmed':True,'target':first['target']})['status'],'succeeded')
        self.assertEqual(self.operation({'action':'remove','confirmed':True,'target':first['target']})['status'],'succeeded')
        self.assertEqual([r['id'] for r in self.pool.accounts.rows()],[second['target']])
        self.assertEqual(self.call('/v1/models',headers=self.auth())[0],200)
    def test_actions_require_same_origin_and_gateway_never_accepts_browser_auth(self):
        self.assertEqual(self.call('/api/operations',{'action':'add','confirmed':True})[0],403)
        self.assertEqual(self.call('/api/operations',{'action':'add','confirmed':True},{'X-Wizard-Account-Action':'1','Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.call('/v1/models',headers={**self.auth(),'Origin':'https://evil.example'})[0],403)
        self.assertEqual(self.call('/v1/models')[0],401)


if __name__ == '__main__': unittest.main(verbosity=2)

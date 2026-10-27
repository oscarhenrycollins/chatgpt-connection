"""App-local account inventory/operations, retaining Oscar's accounts-page protocol.

Provider tokens never enter this module's public rows or operation views.
"""
from __future__ import annotations
import atexit
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

TERMINAL = {'succeeded', 'failed', 'cancelled', 'expired'}


class Refused(ValueError):
    pass


class Accounts:
    def __init__(self, root, provider, adapter):
        self.root, self.provider, self.adapter = root, provider, adapter
        self.mutex = threading.RLock()
        self.jobs = {}
        self.leases = {}
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        atexit.register(self.close)

    def rows(self):
        try:
            rows = json.loads((self.root / 'accounts.json').read_text())
        except FileNotFoundError:
            # Adopt the previous app-owned singleton in place, never copy a refresh chain.
            rows = [{'id': 'legacy', 'label': 'Subscription'}] if self.adapter.has_legacy() else []
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', str(r.get('id', ''))) or not isinstance(r.get('label'), str) for r in rows):
            raise Refused('Account inventory is unreadable; repair app-data before continuing')
        if len({r['id'] for r in rows}) != len(rows):
            raise Refused('Duplicate account IDs')
        return rows

    def save(self, rows):
        self.adapter.atomic('accounts.json', rows)

    def target(self, ident):
        row = next((r for r in self.rows() if r['id'] == ident), None)
        if row is None:
            raise Refused('Account changed; refresh inventory')
        return row

    def public(self):
        with self.mutex:
            result = []
            for row in self.rows():
                state = self.adapter.status(row['id'])
                status = state.get('status', 'not_connected')
                result.append({'id': row['id'], 'nickname': row['label'], 'name': row['label'],
                    'status': status, 'credentialCount': 1, 'plan': state.get('plan'),
                    'account': state.get('account'), 'email': state.get('account'), 'usage': state.get('usage'), 'error': state.get('error'),
                    'credentials': [{'id': row['id'], 'label': row['label'], 'managed': True, 'status': status}],
                    'actions': {'reconnect': True, 'remove': True}})
            connected = any(r['status'] == 'connected' for r in result)
            return {'owner': 'app', 'provider': self.provider, 'status': 'connected' if connected else 'expired' if any(r['status'] == 'expired' for r in result) else 'not_connected',
                'accounts': result, 'actions': {'add': True},
                'management': {'contract': 'wizard.account-management/1', 'status': 'available'},
                'integration': self.adapter.integration(), 'note': 'Sign-ins are stored by this connection app, not your agent.'}

    def view(self, job):
        return {k: job.get(k) for k in ('id', 'provider', 'action', 'target', 'status', 'expiresAt', 'url', 'userCode', 'inputRequired', 'credentialIds')}

    def finish(self, job, status):
        job.update(status=status, url=None, userCode=None, inputRequired=False)
        lease = self.leases.pop(job['id'], None)
        if lease is not None:
            fcntl.flock(lease, fcntl.LOCK_UN)
            os.close(lease)

    def start(self, body):
        action, target, label = body.get('action'), body.get('target'), body.get('label', '')
        if action not in {'add', 'reconnect', 'remove'} or body.get('confirmed') is not True:
            raise Refused('Explicit confirmation required')
        if not isinstance(label, str) or len(label) > 60 or re.search(r'[\x00-\x1f\x7f]|sk-|eyJ', label):
            raise Refused('Invalid nickname')
        with self.mutex:
            self.expire()
            if any(j['status'] not in TERMINAL for j in self.jobs.values()):
                raise Refused('Another account operation is active')
            lease = os.open(self.root / '.account-operation.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                rows = self.rows()
                if action == 'add':
                    if target is not None:
                        raise Refused('Add does not accept a target')
                    row = {'id': 'acct-' + secrets.token_hex(12), 'label': label or 'Subscription'}
                    rows.append(row)
                    self.save(rows)
                else:
                    row = self.target(target)
                job = {'id': secrets.token_urlsafe(24), 'provider': self.provider, 'action': action,
                    'target': row['id'], 'status': 'running', 'expiresAt': time.time() + 900,
                    'url': None, 'userCode': None, 'inputRequired': False, 'credentialIds': []}
                self.jobs[job['id']] = job
                self.leases[job['id']] = lease
            except Exception:
                os.close(lease)
                raise
            for key in list(self.jobs):
                if len(self.jobs) > 64 and self.jobs[key]['status'] in TERMINAL:
                    del self.jobs[key]
            threading.Thread(target=self._begin, args=(job,), daemon=True).start()
            return self.view(job)

    def _begin(self, job):
        try:
            if job['action'] == 'remove':
                self.adapter.disconnect(job['target'])
                with self.mutex:
                    self.save([r for r in self.rows() if r['id'] != job['target']])
                    self.finish(job, 'succeeded')
                return
            challenge = self.adapter.connect(job['target'], lease=self.leases.get(job['id']))
            with self.mutex:
                if job['status'] in TERMINAL:
                    self.adapter.cancel(job['target'])
                    return
                job.update(url=challenge['url'], userCode=challenge.get('code'),
                           inputRequired=self.provider == 'claude', login_id=challenge.get('login_id'))
        except Exception:
            with self.mutex:
                self.finish(job, 'failed')

    def expire(self):
        for job in list(self.jobs.values()):
            if job['status'] not in TERMINAL and time.time() >= job['expiresAt']:
                self.adapter.cancel(job['target'])
                self.finish(job, 'expired')

    def poll(self, ident):
        with self.mutex:
            self.expire()
            job = self.jobs.get(ident)
            if job is None:
                raise Refused('Unknown operation')
            if self.provider == 'chatgpt' and job['status'] == 'running' and job.get('login_id'):
                try:
                    result = self.adapter.poll(job['target'], job['login_id'])
                    if result.get('status') == 'approved':
                        job['credentialIds'] = [job['target']]
                        self.finish(job, 'succeeded')
                        self.adapter.changed()
                except Exception:
                    self.finish(job, 'failed')
            return self.view(job)

    def act(self, body):
        if body.get('action') not in {'cancel', 'submit'}:
            return self.start(body)
        with self.mutex:
            self.expire()
            job = self.jobs.get(body.get('operation'))
            if job is None or job['status'] in TERMINAL:
                raise Refused('Operation is not active')
            if body['action'] == 'cancel':
                self.adapter.cancel(job['target'])
                self.finish(job, 'cancelled')
            else:
                code = body.get('code')
                if self.provider != 'claude' or not job.get('inputRequired') or not isinstance(code, str) or not re.fullmatch(r'[A-Za-z0-9._~#=+/-]{1,2048}', code):
                    raise Refused('Operation is not accepting a code')
                job['inputRequired'] = False
                threading.Thread(target=self._submit, args=(job, code), daemon=True).start()
            return self.view(job)

    def _submit(self, job, code):
        try:
            result = self.adapter.submit(job['target'], code)
            with self.mutex:
                if job['status'] in TERMINAL:
                    return
                if result.get('ok'):
                    job['credentialIds'] = [job['target']]
                self.finish(job, 'succeeded' if result.get('ok') else 'failed')
                if result.get('ok'):
                    self.adapter.changed()
        except Exception:
            with self.mutex:
                if job['status'] not in TERMINAL:
                    self.finish(job, 'failed')

    def close(self):
        with self.mutex:
            for job in list(self.jobs.values()):
                if job['status'] not in TERMINAL:
                    self.adapter.cancel(job['target'])
                    self.finish(job, 'cancelled')

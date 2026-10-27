"""App-owned, unbounded ChatGPT subscriptions and one stock-Hermes gateway."""
from pathlib import Path
import threading
import codex
from accounts import Accounts


class Pool(codex.Connection):
    def __init__(self, data, factory=codex.Connection):
        super().__init__(data)
        self.factory = factory
        self.connections = {}
        self.cursor = 0
        self.selection = threading.Lock()
        self.accounts = Accounts(self.data, 'chatgpt', self)

    def entry(self, ident):
        with self.selection:
            if ident not in self.connections:
                self.connections[ident] = self.factory(self.data if ident == 'legacy' else self.data / 'accounts' / ident)
            return self.connections[ident]

    def has_legacy(self):
        return (self.data / 'connection.json').exists()

    def stored(self):
        if not hasattr(self, 'accounts'):
            return super().stored()
        states = [self.entry(r['id']).stored() for r in self.accounts.rows()]
        return {'status': 'connected' if any(s.get('status') == 'connected' for s in states) else 'expired' if any(s.get('status') in ('expired', 'refreshing') for s in states) else 'not_connected'}

    def status(self, ident):
        entry = self.entry(ident)
        state = entry.stored()
        status = state.get('status', 'not_connected')
        result = {'status': 'expired' if status == 'refreshing' else status}
        if status == 'connected':
            try:
                result['plan'], result['usage'] = entry.usage()
            except codex.Fault as error:
                result.update(status='expired' if error.code in ('expired', 'signed_out') else 'error',
                              error={'code': error.code, 'message': str(error)})
        return result

    def open(self, method, path, body=None, extra=None):
        # Definitive 401/429 only, before headers or stream bytes have been returned.
        rows = self.accounts.rows()
        if not rows:
            raise codex.Fault('signed_out', 'Connect ChatGPT in the ChatGPT app.', 401)
        with self.selection:
            offset = self.cursor % len(rows)
            self.cursor += 1
        last = None
        for row in rows[offset:] + rows[:offset]:
            try:
                return self.entry(row['id']).open(method, path, body, extra)
            except codex.Fault as error:
                if error.code not in ('expired', 'signed_out', 'limit'):
                    raise  # Never replay a network-ambiguous or partially streamed POST.
                last = error
        raise last

    def connect(self, ident, lease=None):
        return self.entry(ident).connect()

    def poll(self, ident, login_id):
        return self.entry(ident).poll(login_id)

    def cancel(self, ident):
        self.entry(ident).cancel()

    def disconnect(self, ident):
        self.entry(ident).disconnect()

    def integration(self):
        import server
        status = server.registration()
        return {'status': status, 'connected': status == 'registered',
                'message': None if status == 'registered' else "Can't reach your agent or its connection is not registered. Sign-ins remain safely stored in this app; retry the agent connection."}

    def changed(self):
        import server
        # Registration is advisory and must not block completion of sign-in.
        threading.Thread(target=server.reconcile, daemon=True).start()


_INSTANCE = None
_INIT = threading.Lock()


def instance():
    global _INSTANCE
    import server
    with _INIT:
        if _INSTANCE is None:
            _INSTANCE = server.owner()
    return _INSTANCE


class Local:
    @classmethod
    def accounts(cls, provider):
        if provider != 'chatgpt':
            raise ValueError('Wrong provider')
        return instance().accounts.public()

    @classmethod
    def request(cls, provider, suffix='', body=None):
        if provider != 'chatgpt':
            raise ValueError('Wrong provider')
        if suffix.startswith('/operations/') and body is None:
            return instance().accounts.poll(suffix.rsplit('/', 1)[-1])
        if suffix == '/operations' and isinstance(body, dict):
            return instance().accounts.act(body)
        return cls.accounts(provider)

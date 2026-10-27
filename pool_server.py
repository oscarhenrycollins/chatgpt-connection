"""Provider UI and narrow authenticated proxy to the Hermes lifecycle broker."""
import re
import base64
import hashlib
import hmac
import json
import os
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from pool_client import BridgeError, REASONS, alongside, agent_address
from account_owner import Local as Hermes
from accounts import Refused
import server as owner

PROVIDER = os.environ.get('WIZARD_POOL_PROVIDER', 'chatgpt')
assert PROVIDER in ('chatgpt', 'claude')
PREFIX = 'WIZARD_' + PROVIDER.upper()
ROOT = Path(__file__).parent / 'pool-static'
PASSWORD = os.environ.get(PREFIX + '_PASSWORD', '')
LEGACY_PREFIX = '/claude-code'
LEGACY = None
LEGACY_RENEW = None
if PROVIDER == 'claude':
    import server as claude_owner
    LEGACY = claude_owner.CONNECTION
    LEGACY_RENEW = claude_owner.renew_if_expired


def is_mirror(row):
    """Hermes' pool copy of the host Claude Code login. Current plugins flag it; older plugins (before the
    flag) list it as an unmanaged grant labelled claude_code, which must not count as a second subscription."""
    if row.get('mirror') == 'claude-code':
        return True
    creds = row.get('credentials') or []
    return bool(creds) and all(c.get('managed') is not True and c.get('name') == 'claude_code' for c in creds)


def same_origin(headers):
    """Browser-only request guard used when no page password is set (a Tailscale-only box): the owner's own
    page may change accounts; a cross-site page may not. Non-browser callers on the app network have no
    Origin and still need the custom action header, which a cross-site form cannot send."""
    origin = headers.get('Origin')
    if origin and urlsplit(origin).netloc != headers.get('Host'):
        return False
    return headers.get('Sec-Fetch-Site') in (None, 'same-origin', 'none')


def masked(email):
    """The plugin's display mask for an email (first letter of each part), so the original Claude Code sign-in can be
    matched against an agent row that only carries the masked form."""
    email = str(email or '')
    if '@' not in email:
        return None
    local, domain = email.rsplit('@', 1)
    return (local[:1] + '***@' + domain[:1] + '***') if local and domain else None


def email_hash(email):
    email = str(email or '').strip().lower()
    return hashlib.sha256(email.encode()).hexdigest()[:16] if '@' in email else None


def same_account(row, email):
    """Whether an agent pool row is the same Claude account as the original sign-in. Exact when the plugin reports an
    email hash; otherwise, on an alongside box only (where that original sign-in is not what the agent uses), the
    masked email must match too."""
    if not email:
        return False
    if row.get('emailHash'):
        return hmac.compare_digest(str(row['emailHash']), email_hash(email) or '')
    return alongside(PROVIDER) and bool(masked(email)) and row.get('email') == masked(email)


def unavailable(error):
    return {'provider': PROVIDER, 'owner': 'app', 'accounts': [],
        'inventoryStatus': 'unavailable', 'actions': {'add': False},
        'management': {'status': 'unavailable'},
        'error': {'code': 'provider_error', 'message': 'App account storage is unavailable; repair app-data before signing in.'}}


def with_legacy(result):
    # The old singleton is adopted in place by the app owner, not mirrored from Hermes.
    result['legacySignin'] = False
    result['accountCount'] = len(result['accounts'])
    return result

def connection(result):
    """An inventory row is not evidence of a usable subscription."""
    rows = result['accounts']
    selected = next((row for status in ('connected', 'ready', 'renewing', 'expired', 'cooldown', 'exhausted', 'error', 'not_connected')
                     for row in rows if row.get('status') == status), {})
    status = selected.get('status', 'error' if rows or result.get('inventoryStatus') == 'unavailable' else 'not_connected')
    if status == 'ready':
        status = 'connected'
    error = selected.get('error') or result.get('error')
    if status in ('cooldown', 'exhausted'):
        status = 'error'
        error = {'code': 'limit', 'message': 'Subscription limit reached; wait for its reset.'}
    elif status == 'error' and not isinstance(error, dict):
        error = {'code': 'provider_error', 'message': 'Subscription state is unavailable.'}
    elif status == 'expired' and not isinstance(error, dict):
        error = {'code': 'expired', 'message': 'Subscription sign-in expired; reconnect it in the app.'}
    answer = {**result, 'contract': 'wizard.ai-connection/2', 'status': status,
              'plan': selected.get('plan'), 'usage': selected.get('usage'),
              'account': selected.get('email') or selected.get('account'), 'error': error}
    if result.get('inventoryStatus') == 'unavailable' and not (rows and selected.get('status') in ('connected', 'ready') and not result.get('alongside')):
        # The agent could not be asked. An empty list here would read as "signed out" on Home; say the agent is
        # unreachable instead, so Home keeps its last reading and shows why.
        answer.pop('accounts', None)
        answer['accountCount'] = None
        answer.update(status='error', plan=None, usage=None, account=None,
                      error={'code': 'unreachable', 'message': (result.get('error') or {}).get('message') or REASONS['unreachable']})
    integration = result.get('integration') or {}
    if integration.get('connected') is False and status == 'connected':
        answer.update(status='error', error={'code': 'unreachable', 'message': integration.get('message') or 'Agent connection is not registered.'})
    try:
        answer['models'] = owner.owner().models() if PROVIDER == 'chatgpt' else owner.inference.models()
    except Exception as error:
        answer['models'] = []
        answer['models_error'] = {'code': 'provider_error', 'message': 'Model catalogue unavailable.'}
    return answer


class Handler(owner.Handler):
    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def json(self, status, data):
        return self.send(status, data)

    def log_message(self, *args):
        pass

    def send(self, status, data, ctype='application/json'):
        if isinstance(data, dict):
            data = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        origins = ["'self'"]
        for origin in os.environ.get('WIZARD_TRUSTED_ORIGINS', '').split(','):
            origin = origin.strip().rstrip('/')
            parsed = urlsplit(origin)
            if parsed.scheme in ('http', 'https') and parsed.netloc and not parsed.path and not parsed.query and not parsed.fragment and not parsed.username:
                origins.append(origin)
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors " + ' '.join(origins) + "; base-uri 'none'; form-action 'none'")
        if origins == ["'self'"]:
            self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def authorized(self, path=None):
        if urlsplit(self.path).path == '/api/connection' and self.headers.get('X-Wizard-Grant'):
            return super().authorized('/api/connection')
        if not PASSWORD:
            return True
        try:
            scheme, value = self.headers.get('Authorization', '').split(' ', 1)
            given = base64.b64decode(value).decode().partition(':')[2]
            if scheme == 'Basic' and hmac.compare_digest(given.encode(), PASSWORD.encode()):
                return True
        except (ValueError, UnicodeError):
            pass
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Subscriptions"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def legacy(self, method):
        """The original Claude Code sign-in page (Connect, code, Test, Sign out) under claude-code/: the login
        the host agent's Claude Code provider uses. Kept so that login can always be renewed from this app."""
        if PROVIDER != 'claude':
            return self.send(404, {'error': 'not found'})
        path = urlsplit(self.path).path
        if path == LEGACY_PREFIX:
            self.send_response(308)
            self.send_header('Location', 'claude-code/')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        rest = self.path[len(LEGACY_PREFIX):] or '/'
        self.path = rest
        return method(self)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == LEGACY_PREFIX or path.startswith(LEGACY_PREFIX + '/'):
            self.send_response(308)
            self.send_header('Location', '../' if path.endswith('/') else './')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if path.startswith('/v1/') and PROVIDER == 'chatgpt':
            return owner.Handler.do_GET(self)
        if path == '/health':
            return self.send(200, {'status': 'ok', 'version': owner.VERSION})
        if not self.authorized():
            return
        if path == '/.well-known/mcp/server-cards.json':
            card = owner.card(self.headers.get('Host', '')) if PROVIDER == 'claude' else {'name': 'wizard-chatgpt', 'version': owner.VERSION, 'x-wizard': {'contract': 'wizard.ai-connection/2', 'secrets_exposed': False}}
            card['x-wizard']['credential_owner'] = 'connection app (app-data)'
            return self.send(200, card)
        if path == '/SKILL.md':
            return self.send(200, (ROOT.parent / 'SKILL.md').read_bytes(), 'text/markdown; charset=utf-8')
        if re.fullmatch(r'/api/operations/[A-Za-z0-9_-]{20,64}', path):
            if not PASSWORD and not same_origin(self.headers):
                return self.send(403, {'error': 'Cross-site request rejected'})
            try:
                return self.send(200, Hermes.request(PROVIDER, '/operations/' + path.rsplit('/', 1)[1]))
            except BridgeError as error:
                return self.send(error.status, {'error': 'Operation status unavailable; check inventory'})
            except Exception:
                return self.send(503, {'error': 'Operation status unavailable; do not restart the operation'})
        if path in ('/api/accounts', '/api/connection'):
            if path == '/api/connection' and any(k.lower() in ('tailscale-user-login', 'x-forwarded-for', 'forwarded', 'x-real-ip') or k.lower().startswith('cf-') for k in self.headers):
                return self.send(403, {'error': 'box-only'})
            try:
                result = with_legacy(Hermes.accounts(PROVIDER))
            except BridgeError as error:
                # Existing subscriptions are unchanged; the page says why and offers Retry, never a sign-in that
                # would not reach the agent.
                result = with_legacy(unavailable(error))
            except Exception:
                result = with_legacy(unavailable(BridgeError()))
            if path == '/api/connection':
                result = connection(result)
            return self.send(200, result)
        assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/app.css': ('app.css', 'text/css; charset=utf-8'), '/boot.js': ('boot.js', 'text/javascript; charset=utf-8'), '/theme-client.js': ('theme-client.js', 'text/javascript; charset=utf-8'), '/wizard-ui.css': ('wizard-ui.css', 'text/css; charset=utf-8')}
        if path in assets:
            name, ctype = assets[path]
            return self.send(200, (ROOT / name).read_bytes(), ctype)
        return self.send(404, {'error': 'not found'})

    def do_POST(self):
        if urlsplit(self.path).path.startswith('/v1/') and PROVIDER == 'chatgpt':
            return owner.Handler.do_POST(self)
        if urlsplit(self.path).path == '/api/infer':
            # Preserve main's router grants, isolated providers and SSE contract.
            return owner.Handler.do_POST(self)
        path = urlsplit(self.path).path
        if path.startswith(LEGACY_PREFIX + '/'):
            return self.send(410, {'error': 'Use the accounts page to manage this sign-in.'})
        if not self.authorized():
            return
        if not PASSWORD and not same_origin(self.headers):
            # No page password (Tailscale-only box, like the original Connect): same-origin browser requests only.
            return self.send(403, {'error': 'Cross-site action rejected'})
        if urlsplit(self.path).path != '/api/operations':
            return self.send(404, {'error': 'not found'})
        if self.headers.get('X-Wizard-Account-Action') != '1' or self.headers.get('Content-Type', '').split(';')[0] != 'application/json' or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return self.send(403, {'error': 'Cross-site action rejected'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 4096:
                return self.send(413, {'error': 'Request too large'})
            body = json.loads(self.rfile.read(size))
            if not isinstance(body, dict) or set(body) - {'action', 'target', 'label', 'confirmed', 'operation', 'code'}:
                return self.send(400, {'error': 'Invalid request'})
            inventory = Hermes.accounts(PROVIDER)
            management = inventory.get('management') or {}
            if management.get('contract') != 'wizard.account-management/1' or management.get('status') != 'available':
                return self.send(403, {'error': 'Account changes unavailable; subscriptions are unchanged'})
            action = body.get('action')
            if action == 'add':
                permitted = (inventory.get('actions') or {}).get('add') is True
            elif action in ('reconnect', 'remove'):
                permitted = any((row.get('actions') or {}).get(action) is True and
                    any(c.get('id') == body.get('target') and c.get('managed') is True for c in row.get('credentials', []))
                    for row in inventory.get('accounts', []))
            else:
                permitted = action in ('submit', 'cancel')
            if not permitted:
                return self.send(403, {'error': 'Account action unsupported'})
            return self.send(200, Hermes.request(PROVIDER, '/operations', body))
        except Refused:
            return self.send(409, {'error': 'Account operation refused; refresh inventory before retrying.'})
        except BridgeError as error:
            if error.status == 409:
                return self.send(409, {'error': 'Another sign-in is running, or the request was refused; refresh first'})
            return self.send(503, {'error': REASONS.get(error.code, REASONS['unreachable']), 'code': error.code})
        except Exception:
            return self.send(503, {'error': 'Operation unavailable; refresh inventory before retrying'})

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def server_bind(self):
        # HTTPServer's reverse-DNS lookup can stall an isolated container startup.
        from socketserver import TCPServer
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


if __name__ == '__main__':
    import signal
    import threading
    http = Server((os.environ.get('WIZARD_POOL_BIND', owner.BIND), int(os.environ.get('WIZARD_POOL_PORT', owner.PORT))), Handler)
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=http.shutdown, daemon=True).start())
    try:
        http.serve_forever()
    finally:
        http.server_close()
        if PROVIDER == 'claude':
            owner.inference.FAST_SPARE.close()

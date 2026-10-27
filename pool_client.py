"""Authenticated dashboard bridge; provider credentials never leave Hermes."""
import http.cookiejar
import json
import os
import threading
import urllib.request
import urllib.error
from urllib.parse import urlsplit

DEFAULT_URL = 'http://hermes-agent:9119'

# Why the agent's account broker could not be read. The page shows the line and a Retry; it never offers a sign-in
# that would not reach the agent (see pool_server.legacy_signin).
REASONS = {
    'not_configured': "This app isn't linked to your agent yet. Ask your agent to reconnect it to itself.",
    'unreachable': "Can't reach your agent right now. Retry in a moment; if it keeps failing, ask your agent to "
                   "reconnect this app.",
    'login_rejected': "Your agent didn't accept this app's login. Ask your agent to reconnect this app.",
    'no_broker': "Your agent can't add sign-ins from this app yet.",
}


class BridgeError(RuntimeError):
    def __init__(self, status=503, code='unreachable'):
        super().__init__('Hermes operation unavailable')
        self.status = status if status in (404, 409, 413) else 503
        self.code = code if code in REASONS else 'unreachable'


def agent_url(provider):
    return os.environ.get('WIZARD_' + provider.upper() + '_HERMES_URL', '').strip().rstrip('/') or DEFAULT_URL


def alongside(provider):
    """True when the app drives a Hermes on the host (alongside mode), not the Hermes Agent app."""
    return agent_url(provider) != DEFAULT_URL


def agent_address(provider):
    """host:port of the agent dashboard this app talks to (never credentials), for the error line."""
    try:
        parts = urlsplit(agent_url(provider))
        return (parts.hostname or '') + (':%d' % parts.port if parts.port else '')
    except (TypeError, ValueError):
        return ''


class Hermes:
    lock = threading.Lock()
    opener = None

    @classmethod
    def request(cls, provider, suffix='', body=None):
        prefix = 'WIZARD_' + provider.upper()
        origin = agent_url(provider)
        with cls.lock:
            if cls.opener is None:
                password = os.environ.get(prefix + '_HERMES_PASSWORD', '')
                if not password:
                    raise BridgeError(503, 'not_configured')
                opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
                data = json.dumps({'provider': 'basic', 'username': os.environ.get(prefix + '_HERMES_USER', 'hermes'), 'password': password}).encode()
                request = urllib.request.Request(origin + '/auth/password-login', data=data, headers={'Content-Type': 'application/json'}, method='POST')
                try:
                    with opener.open(request, timeout=15) as response:
                        response.read()
                except urllib.error.HTTPError as error:
                    status = error.code
                    error.close()
                    # 401/403: wrong password; 404: something that is not the agent's dashboard answered.
                    raise BridgeError(503, 'login_rejected' if status in (401, 403, 404) else 'unreachable') from None
                except Exception:
                    raise BridgeError(503, 'unreachable') from None
                cls.opener = opener
            try:
                req = urllib.request.Request(origin + '/api/plugins/wizard-os/provider-accounts/' + provider + suffix,
                    data=json.dumps(body).encode() if body is not None else None,
                    headers={'Content-Type': 'application/json', 'X-Wizard-Account-Action': '1'},
                    method='POST' if body is not None else 'GET')
                with cls.opener.open(req, timeout=45) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                cls.opener = None
                status = error.code
                error.close()
                # A 404 on the inventory itself: the agent has no Wizard OS account broker (older plugin or the
                # fresh-mode agent). 401: the dashboard session lapsed; the next call logs in again.
                code = 'no_broker' if status == 404 and not suffix else 'login_rejected' if status in (401, 403) and not suffix else 'unreachable'
                raise BridgeError(status, code) from None
            except Exception:
                cls.opener = None
                # Never automatically retry a start/submit/remove after ambiguity.
                raise BridgeError(503, 'unreachable') from None

    @classmethod
    def accounts(cls, provider):
        result = cls.request(provider)
        if result.get('owner') != 'hermes' or result.get('provider') != provider or not isinstance(result.get('accounts'), list):
            raise BridgeError(503, 'no_broker')
        return result

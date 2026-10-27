# Read-only by design: signing in, adding, reconnecting and removing subscriptions are owner actions on the
# ChatGPT page. The MCP only reports the wizard.ai-connection/2 answer (overall status, plan, usage and one row per
# subscription), never a token.
# Box-only: a call that came through Tailscale Serve, a tunnel or a proxy is refused; agents reach this
# MCP on the app network (http://wizard-chatgpt:8801/mcp), which adds none of these headers.

PROXY_HEADERS = ("tailscale-user-login", "x-forwarded-for", "forwarded", "x-real-ip")


@tool("connection_status", "ChatGPT subscription status on this Wizard: connected / not_connected / expired / error, "
      "the plan, the account label, current usage limits (percent used, percent left and reset time), and `accounts`: "
      "one row per ChatGPT subscription signed in to the agent, with its own status and usage. When the app can't "
      "reach the agent, status is error with error.code unreachable / not_configured / login_rejected. "
      "Read-only; never returns a token.")
def connection_status():
    if any(k in PROXY_HEADERS or k.startswith("cf-") for k in REQUEST.get()):
        raise PermissionError("box-only: call this MCP from the server (app network), not through the tailnet or a proxy")
    headers = {}
    auth = REQUEST.get().get("authorization")
    if auth:
        headers["Authorization"] = auth  # the page password (Basic), forwarded unchanged
    _, raw = http("GET", "/api/connection", None, headers, timeout=15)
    d = json.loads(raw)
    code = (d.get("error") or {}).get("code")
    if d.get("status") == "not_connected":
        d["next"] = ("Ask the owner to open the ChatGPT app and press Connect (or Add account). ChatGPT first needs Settings -> Security -> "
                     "Device code sign-in for Codex turned on; then he copies the code, opens the sign-in link and pastes it. "
                     "Never ask for the code or a token in chat.")
    elif d.get("status") == "expired":
        d["next"] = "Ask the owner to open the ChatGPT app and tap the expired account's row and press Reconnect."
    elif code in ("unreachable", "not_configured", "login_rejected"):
        d["next"] = ("The ChatGPT app can't reach this agent's dashboard (error.message; the page shows the agent address). "
                     "On a box where Wizard runs next to your own Hermes, run `wam alongside` and then any `wam app update ...` "
                     "it prints (re-points the app at this agent and restarts it); otherwise `wam app update wizard-chatgpt`. "
                     "Check again; tell the owner in plain words if it still fails.")
    elif code == "limit":
        d["next"] = "That subscription hit its limit; the owner can add another ChatGPT subscription with Add account in the ChatGPT app."
    return d

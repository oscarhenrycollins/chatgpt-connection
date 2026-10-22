# ChatGPT

Connect your ChatGPT subscription once; your agent signs in with it and Wizard shows it as connected.

A [Wizard app](https://www.aiwizards.com/wizard-apps) (System App) and the ChatGPT **AI connection** app under decision #61 (one connection app per AI provider). Open its page, press **Connect**, open the ChatGPT link and enter the short code. The page finishes by itself.

The sign-in is **Hermes Agent's own ChatGPT (Codex) device-code login**, started through the agent's stock dashboard API with the dashboard login the app was installed with. Hermes stores and renews the tokens. Codex refresh tokens are single-use, so this app never holds, copies or refreshes one. Connecting never moves a working agent off its current account; **Use ChatGPT** does that on request.

## Contract: `wizard.ai-connection/2`

`GET /api/connection` (box-only, never a token, cached 60 s):

```json
{"contract": "wizard.ai-connection/2", "provider": "chatgpt", "name": "ChatGPT",
 "status": "connected", "plan": null, "account": "you@example.com", "usage": null, "error": null}
```

- `status` is `connected | not_connected | error`.
- `usage` comes from the existing Wizard OS plugin usage route when available, otherwise remains `null`. Hermes alone reads its own credentials.
- Requests carrying `tailscale-user-login`, `x-forwarded-for`, `forwarded`, `x-real-ip` or `cf-*` are refused (403).

## Where it works

- **Hermes Agent app from Wizard Apps (fresh Wizard):** works. `wam install wizard-chatgpt` fills the dashboard login from that app (`uses: [hermes-agent]`).
- **Alongside (your own Hermes on the host):** status reads `error` with a plain message. The host Hermes runs no persistent dashboard, and the rootless-Docker app can't reach host loopback. Sign in on the host with `hermes auth add openai-codex`; Wizard still shows your ChatGPT usage from that sign-in.

## Connection Router (`wizard.ai-connection/2`, decision #65)

Implements [spec/CONNECTION_ROUTER.md](https://github.com/humanitylabs-org/wizard-os/blob/main/spec/CONNECTION_ROUTER.md).
The `/1` status/plan/account/usage fields keep their existing semantics; `/2` adds
`models: [{id, label, tier, abilities}]` (context size is optional and not guessed).
These are configured model allowlists, not entitlement probes. Unavailable models
fail closed; there is no account, model or provider fallback.

`POST /api/infer` accepts `{model, system?, messages, max_tokens?, stream?, abilities?}`.
Messages are nonempty lists of exact `{role: "user"|"assistant", content: string}`.
Only streaming is supported (`stream: true` or omitted), with SSE `delta` text,
then `done` carrying the **provider-reported effective model** and usage. Errors use
`signed_out|expired|limit|unreachable|provider_error`. Missing done is failure;
discard provisional text on an error. No arbitrary tools, profile, URL or headers
are accepted in the body. No inference request/response content is logged or saved.

### Router grants and exposure

`WIZARD_GRANTS_FILE` uses the router's configuration shape:

```json
{"enforce": true, "incoming": [{"from": "wizard-connection-router", "sha256": "<64 lowercase hex SHA-256 of the opaque router grant>"}]}
```

The router sends that opaque token in `X-Wizard-Grant`; configure its `outgoing`
entry under this provider's app ID. Known grants belonging to other apps are
refused. Missing/malformed grant files fail closed. With no file, or `enforce:false`,
unknown grants are **report-only**, matching the permissions rollout: network peers
are not authenticated as the router. `X-Wizard-App` is never authentication.
Do not expose this port to untrusted peers until enforcement is enabled. Browser
Origin and proxy-identity headers are refused even with a valid grant. A recognized router grant also permits discovery when the owner page has a password;
unknown/report-only grants do not bypass that password. The owner
page password is not an inference credential. Keep owner routes behind their
existing access controls; router traffic stays on the private app network.

### Verification and release

Run the offline tests below; they use only loopback fixtures, never real sign-ins.
Container builds/tests must run on the release builder, not the control-plane VPS.
This source PR does not publish engine images or change the existing pinned image
digests in compose. Before store rollout, the maintainer must build/publish these
sources and update the engine digest; merging metadata alone does not deploy `/2`.

### ChatGPT implementation

Discovery comes from Hermes `GET /api/plugins/wizard-os/models/chatgpt`.
Inference forwards to `POST /api/plugins/wizard-os/infer/chatgpt`, using this
app's existing authenticated dashboard cookie session. No ChatGPT token is ever
read or copied. The router's caller headers are not forwarded; `abilities` are
validated against the plugin catalogue, then removed because the plugin schema
accepts only model/system/messages/max_tokens/stream. No agent API is called.

Requires **Hermes plugin wizard-os >=0.4.0**, hosted in-process (host isolation
buffers SSE and is unsupported by the plugin). A missing discovery/inference route
returns `provider_error` with `Hermes plugin wizard-os >=0.4.0 required`.
Delta and done SSE frames pass through unchanged; nested plugin errors are
normalized to the router's top-level code shape. Missing/mismatched done fails.
No redirect or inference POST replay is allowed. Two active streams, 256 KiB
request/frame bounds, 4 MiB total stream and 120 s stream deadline bound resources.
Heartbeats let disconnects cancel the upstream response; cancellation shuts down
the urllib response socket before closing its buffered reader (retest this stdlib
seam on Python upgrades). Upstream connection/header wait is bounded at 120 s.
The plugin imposes its own tighter total inference and local token/byte guards.

```sh
python3 test-chatgpt.py
python3 test-inference.py -v
python3 mcp/inline.py --check
```

No live plugin install, dashboard restart or real ChatGPT call is part of these tests.

## Files

- `wizard.md`: what the app is and what it gives the agent (spec: wizard.md v0.2).
- `runtipi/`: stock Runtipi `config.json` and `docker-compose.yml` (engine pinned by digest).
- `mcp/`: a read-only MCP (`connection_status`), inlined into the compose file by `python3 mcp/inline.py`.
- `server.py`, `static/`, `Dockerfile`: the page image `ghcr.io/humanitylabs-org/wizard-chatgpt`, built and published from `humanitylabs-org/wizard-app-store` `images/wizard-chatgpt`.
- `test-chatgpt.py`, `test-container.sh`: the stand-in-dashboard and container tests CI runs before publishing.
- `SKILL.md`: the skill the agent loads when this app is installed.
- `.github/workflows/publish.yml`: every push to `main` re-publishes to the index.

Replaces the ChatGPT part of [AI Accounts](https://github.com/oscarhenrycollins/ai-accounts). For your own subscription on your own server.

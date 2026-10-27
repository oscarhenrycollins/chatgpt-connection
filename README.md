# ChatGPT connection

**Canonical upstream: https://github.com/oscarhenrycollins/chatgpt-connection.**
The humanitylabs-org repository is a contribution fork, not a separate release line. This upstream-based branch ports app-owned login onto Oscar's latest multi-subscription release; it does not ask upstream to adopt the fork wholesale.

## Candidate 0.3.1

James's decision: “The login should not live inside the agent. The login should live inside the connection apps.”

Open this app to sign in, add any number of subscriptions, reconnect expired ones or remove a selected sign-in. One accounts page retains Oscar's rows, usage meters, copy-first steps, icons, and missing-agent diagnostics. Provider status is separate from consumer readiness: a successful local login is not proof that an alongside agent uses it.

- Shipped accounts-page/engine baseline: `humanitylabs-org/wizard-app-store` at `254ee6f2`, the source of Oscar's pinned release images. The previously checked-in app server was not the shipped engine.
- The recipe is now an explicit source package over the same pinned upstream interpreter/native CLI image. It prepares root-owned app-data binds for UID 1000, mounts source read-only and keeps the engine unprivileged.
- Auth and renewal are app-owned. Each OAuth subscription has an isolated folder beneath `data/accounts`. The existing app-owned singleton is adopted in place. Stock Hermes uses a box-local bearer via the app native Codex gateway, never the provider refresh chain.
- Existing agent-owned sign-ins are **not** silently copied, deleted or refreshed from a second owner. See the exclusive migration requirements below.
- Release pins are unchanged. This candidate is not deployed to the owner's box and is not yet a release-ready migration.

## Onboarding

“Log in with your subscription” should open this installed app. It must not start `hermes auth` in the agent. Add/Reconnect/Remove stay on this page. Connect Tailscale is unaffected.

## Design, migration and evidence

Read [the design note](docs/APP_OWNED_LOGIN.md). Existing agent-owned subscriptions need an explicit stopped-owner transfer or ordinary re-sign-in; the old refreshing owner must be retired before the new one is enabled. A snapshot/copy of a live rotating credential is not a migration.

Offline tests run synthetic providers and a CLI fixture, never the owner's credentials. Their results prove the protocol, persistence and failure paths, not paid-provider OAuth acceptance or an installed-box migration. Draft status must remain until the disposable Wizard/Hermes install and consumer-grant checks pass. No live-box install, service restart, release-pin change or upstream merge is authorized here.

## Tests

```sh
python3 test-accounts.py
python3 test-pool.py
python3 test-chatgpt.py
python3 test-inference.py
python3 mcp/inline.py --check
```

The app still exposes `wizard.ai-connection/2`, a read-only `connection_status` MCP tool and the restricted `/api/infer` route for an owner-granted Connection Router. The native Codex gateway additionally supports models, Responses, compaction and usage on `/v1/*`; browser/forwarded requests never authenticate to that gateway.

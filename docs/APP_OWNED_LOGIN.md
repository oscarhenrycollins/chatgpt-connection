# App-owned subscription sign-in — upstream port

Owner decision, James, 27 October 2026: **“The login should not live inside the agent. The login should live inside the connection apps.”**

## Baseline and scope

Start at Oscar's Claude 0.2.6 (`d0b59dd3`) and ChatGPT 0.2.4 (`476d771f`), not the divergent fork mains. Those recipes actually execute `wizard-app-store` engines at `254ee6f2`; the app repositories' older server/static sources are not the shipped accounts pages. Bring that exact engine/page source into each source package before extending it. Preserve upstream icons, unlimited account rows, one accounts page, copy-first device steps, existing status/usage contracts, missing-agent diagnostics and alongside safety.

## Credential owner and consumers

Claude runs official Claude Code sign-in per subscription beneath its own `data/claude` directory. Export the entire folder as `claude-login`, including CLI refresh locks. A consumer may use it only under an App Manager share grant. Never bind the owner's host login into the app as an implicit fallback. Existing root `.credentials.json` remains supported; additional accounts have opaque IDs and separate CLI config directories. A consumer must explicitly support this directory inventory before claiming multi-account rotation. Subscription intermediation compliance is unresolved; using the official CLI alone is not a compliance certification.

ChatGPT reuses the fork's app-owned Codex OAuth/rotating-token implementation, one isolated connection per subscription under app-data. Only the app refreshes provider credentials. Stock Hermes consumes a local bearer through the app's native Codex gateway; it never gets a provider access/refresh token. Model catalogue, quota, compaction, native tools and unchanged default-model selection remain part of the contract. Fail over only on definitive authentication/quota rejection before streaming starts; never retry an ambiguous POST or a partially emitted reply.

The accounts page keeps Oscar's operation/inventory protocol. It now talks to an app-local owner instead of driving the agent's `hermes auth`. The agent dashboard is used for consumer registration/readback only. Missing consumer registration is distinct from a successful provider login: show the agent problem and never report that an alongside agent is connected merely because an unused local credential exists.

## Onboarding

“Log in with your subscription” opens the installed Claude or ChatGPT app, not the agent dashboard. Add/Reconnect/Remove work in that same page for any number of subscriptions. The app says separately whether the agent can use the connection. Connect Tailscale is unchanged. Release pins are Oscar's decision and are not changed here.

## Existing boxes and exclusive migration

Do not silently copy or delete agent credential pools. Stop callers and the old credential owner first. Export/move each selected subscription to the connection app using an explicit operator migration with a durable journal, atomic persistence, file/directory fsync and readback; only then remove the old refreshing owner. Ambiguous or failed handover must leave both sides stopped and require review. Offer ordinary app re-sign-in as the safe fallback; it preserves old account inventory until the owner explicitly removes it. Never start two refreshers from one rotating chain. Existing app-owned ChatGPT singleton data is adopted in place, not copied. Legacy Claude named-volume and host-bind migrations also require an exclusive move; a recipe change alone is not proof of migration.

WAM must preserve existing share grants (draft #215 is relevant but not sufficient proof), recognize app-owned declarations before applying alongside host-login overrides, and compile consumer share mounts. Hermes' Claude consumer needs directory-inventory selection/rotation; ChatGPT needs app-labelled local-bearer registration without changing the current model or redirecting other Codex credentials. The old dashboard broker must not remain onboarding's credential owner.

## Acceptance and evidence boundaries

Use forum, native Linux amd64, disposable app-data and synthetic providers only. Exercise real source-package startup, two accounts, restart/readback, renewal/exhaustion, expired/reconnect, removal, denied/cross-site requests and missing consumer. A fake provider or CLI is a contract test, not a real paid-subscription proof. Live provider login/refresh is an owner-only gate if no authorized synthetic account exists. Never read James's credentials, deploy to his box, change release pins, or mark draft #215 ready without its migration/grant proof.

Reuse:
- accounts page/operations: EXTEND Oscar's shipped `pool_server.py`, `pool-static/*` from wizard-app-store 254ee6f2; retain all account actions and state.
- provider auth: REUSE installed connection apps, existing `codex.Connection` and official Claude Code CLI.
- skin/Back/update holds: REUSE `WizardApp` from `/wizard-os-home/core/app-client.js`; remove inherited per-app root guards rather than add another implementation.
- rows/meters: REUSE shell Wizard UI kit and semantic `--wz-*` tokens.
- consumer grants: EXTEND WAM's declared shares and grants compiler, not guessed sibling paths.
Visual: preserve upstream page and icons; browser evidence must distinguish synthetic fixture tests from actual installed-shell coverage.

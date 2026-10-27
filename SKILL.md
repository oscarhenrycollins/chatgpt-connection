---
name: wizard-chatgpt
description: Use when checking the installed ChatGPT connection app. Sign-in belongs to the app, not the agent.
---
# ChatGPT

- Open the installed app for Sign in, + Add, Reconnect and Remove. Never run `hermes auth add` to onboard this app.
- Read `GET /api/accounts` for app-owned subscriptions and separate `integration` consumer status. `GET /api/connection` must not claim a working agent if only a local provider login exists.
- Never print, copy into chat, or refresh provider tokens. Never silently migrate the agent's existing credential pool. Re-sign-in or an exclusive stopped-owner handover is required.
- Register only the app-labelled local gateway bearer with stock Hermes; keep provider OAuth credentials inside app-data and never change the default model implicitly.
- Preserve all subscriptions, page controls, icons, usage reporting and missing-agent diagnostics. Source and canonical release repository: `oscarhenrycollins/chatgpt-connection`.
- Candidate installation and migration prerequisites are in `docs/APP_OWNED_LOGIN.md`. Synthetic tests do not certify a live provider account.

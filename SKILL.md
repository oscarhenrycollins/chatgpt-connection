---
name: wizard-chatgpt
description: Use when the owner wants his agent on this Wizard server to use his ChatGPT subscription, or asks whether ChatGPT is connected. The ChatGPT app starts the agent's own ChatGPT sign-in; agents never handle the token.
---

# ChatGPT (Wizard AI connection app)

The ChatGPT app is where the owner connects his ChatGPT subscription (Plus or Pro) once for the
server (decision #61: one connection app per AI provider). It replaces the ChatGPT part of AI Accounts.

## What the agent must know

- The sign-in is done **by the owner, on the ChatGPT app's page** (`http://<server>:8800`): press
  **Connect**, open the ChatGPT link, enter the short code shown, approve. The page finishes by itself.
  Never ask the owner for a code, token or password in chat, and never try to sign in yourself.
- The sign-in runs inside Hermes Agent (its own ChatGPT device-code login), so Hermes keeps and renews
  the tokens. Never read, copy, print or send Hermes' `auth.json` or any ChatGPT token: ChatGPT refresh
  tokens are single-use, so a copy signs the agent out.
- Status (read-only, never a token): the MCP tool `connection_status`, or
  `GET http://wizard-chatgpt:8800/api/connection` from the app network. It returns the
  `wizard.ai-connection/2` shape: `status` (`connected`, `not_connected`, `expired`, `error`), `account`
  when known, `usage` from the plugin when available (otherwise null), and `error` with a plain message.

## Switching the agent to ChatGPT

Connecting never moves a working agent off its current account. If the agent had no working account,
connecting switches it to ChatGPT; otherwise the page shows **Use ChatGPT**. Don't switch the provider
or model unless the owner asks.

## When status is `error`

The app needs the Hermes Agent app from Wizard Apps (it drives that agent's dashboard). On a server
where the owner's own Hermes runs outside Wizard Apps, the page says so: the owner signs in there with
`hermes auth add openai-codex` (or `hermes model`), and Wizard still shows ChatGPT usage from it.

## Isolated model calls (decision #65)

- Apps declare `requires.models` and call the Connection Router, not this provider directly.
- `/api/connection` now adds a `models` catalogue; existing status, plan, account and usage fields remain.
- `/api/infer` is a router backend, not an agent API: explicit text only, no persona, memory or session persistence.
- Text deltas are provisional. Only a final `done` with the approved effective model is success.
- Never work around `signed_out`, `expired`, `limit`, `unreachable` or `provider_error` by switching accounts/models.
- Grants are report-only until enforcement is configured. Keep the app on a trusted private network; see README.

## Limits

- One agent per app: the ChatGPT sign-in belongs to the Hermes Agent this app is installed with.
- Personal use with the owner's own subscription.

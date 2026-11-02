---
wizard: 0.2
id: wizard-chatgpt
name: ChatGPT
type: external
category: system
tags: [security]
version: 0.2.17
summary: Connect one or more ChatGPT subscriptions; your agent uses them, and you see how much each has left.
icon: icon.png
license: MIT
source: https://github.com/oscarhenrycollins/chatgpt-connection
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
mcp:
  path: /mcp
  port: 8801
  transport: streamable-http
skill: SKILL.md
view:
  path: /
  port: 8801
provides:
- ai-connection
- chatgpt-subscription
uses:
- hermes-agent
health: /health
browser_contract:
  version: 1
  isolation: opaque
  operations: [ai_accounts.inventory, ai_accounts.change, ai_accounts.progress, ai_accounts.step]
  navigation:
  - {id: signin, url: 'https://auth.openai.com/codex/device'}
---

# ChatGPT

Connect your ChatGPT Plus or Pro subscriptions on the ChatGPT page, and your agent uses them. Each sign-in is the agent's own ChatGPT device-code login (`hermes auth add openai-codex`, run by the agent's dashboard), so the agent keeps and renews the tokens and this app never sees them. ChatGPT asks you once to turn on **Settings → Security → Device code sign-in for Codex**; the page says so before it shows the code.

This is a Wizard AI connection app (`wizard.ai-connection/2`): `GET /api/connection` reports the overall status, plan and usage plus one row per subscription, `GET /api/accounts` lists them, and the MCP tool `connection_status` gives agents the same answer. When the app can't reach the agent it says so (with the agent address) instead of showing an empty list. Engine source: `humanitylabs-org/wizard-app-store` `images/wizard-chatgpt`; the page is the same one the Claude app uses.

**Browser boundary (draft, default-off).** `browser_contract` asks for the reviewed account operations and the provider's own sign-in page; nothing is granted until the owner grants it, and nothing changes unless `WIZARD_BROWSER_BOUNDARY` is on. With it on, the accounts routes answer only the Agent Service adapter (the MCP front's listener gate), account changes run only after the owner confirms them in a Wizard dialog, and only Wizard opens the sign-in page (wizard-os `spec/browser-trust-shell-intents`).

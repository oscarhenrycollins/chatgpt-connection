---
wizard: 0.2
id: wizard-chatgpt
name: ChatGPT
type: external
category: system
tags: [security]
version: 0.3.1
summary: Connect one or more ChatGPT subscriptions; your agent uses them, and you see how much each has left.
icon: icon.png
license: MIT
source: https://github.com/oscarhenrycollins/chatgpt-connection
runtime:
  kind: runtipi
  config: runtipi/config.json
  compose: runtipi/docker-compose.yml
  package:
    mode: source
    root: .
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
---

# ChatGPT

Sign in with your subscription in this connection app. Add as many subscriptions as you need on one accounts page. Sign-in and renewal live in private app-data, not your agent. The page reports provider status separately from whether the agent can use it, so an alongside box cannot mistake an unused local login for a working agent connection.

See `docs/APP_OWNED_LOGIN.md` for the design, onboarding integration, exclusive migration requirements, and evidence boundaries. Existing agent-owned subscriptions are not silently copied or deleted.

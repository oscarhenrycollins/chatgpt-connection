---
wizard: 0.2
id: wizard-chatgpt
name: ChatGPT
type: external
category: system
tags: [security]
version: 0.2.0
summary: Connect your ChatGPT subscription once; your agent signs in with it and Wizard shows it as connected.
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
---

# ChatGPT

Connect your ChatGPT Plus or Pro subscription once on the ChatGPT page, and your Hermes Agent uses it. The sign-in is the agent's own ChatGPT device-code login. Hermes keeps and renews the tokens, so this app never sees them.

This is a Wizard AI connection app (`wizard.ai-connection/2`): `GET /api/connection` reports whether ChatGPT is connected, and the MCP tool `connection_status` gives agents the same answer. Usage (plan and limits) comes from the agent's own read through the Wizard OS Hermes plugin route, so this app still never holds a token; on an agent without that route usage is `null`. It replaces the ChatGPT part of AI Accounts.

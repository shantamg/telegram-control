---
name: telegram-session-control
description: Show the real controller-owned Telegram agent/provider session controls in the current topic from an active Telegram Control-managed Codex or Claude turn. Use implicitly when the user asks to see Telegram topic agent controls, manage this topic's Codex or Claude conversation, pause or resume its provider agent, start a fresh provider session, resume another provider session, or asks the agent to open or invoke the /agent interface. Do not confuse these with domain-specific task, monitoring, email, or application controls. The user does not need to mention this skill by name.
---

# Telegram Session Control

Request the current topic's validated controller interface:

```bash
/usr/bin/python3 /Users/shantam/telegram-control/agent_telegram.py controls
```

Use this only inside a Telegram Control-managed turn running in the agent's
own topic. Call it immediately before the normal final response so the current
turn can finish before the user confirms a session change.

The controller creates the authorized one-time buttons and performs every
validation. Do not modify controller state, simulate an inbound `/agent`
message, or claim that a session changed. If the helper rejects a relayed turn,
tell the user to open the agent's own topic and ask there or send `/agent`.

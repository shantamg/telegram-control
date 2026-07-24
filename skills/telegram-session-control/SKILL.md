---
name: telegram-session-control
description: Show the controller-owned agent and session controls in the current Telegram topic from an active Telegram Control-managed Codex or Claude turn. Use implicitly when the user asks to see agent controls, manage this topic or conversation, pause or resume its agent, start a fresh session, resume another session, or asks the agent to open or invoke the /agent interface. The user does not need to mention this skill by name.
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

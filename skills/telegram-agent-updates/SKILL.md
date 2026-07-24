---
name: telegram-agent-updates
description: Send a concise text or voice update to the current Telegram topic from an active Telegram Control-managed Codex or Claude turn. Use when the user asks for a Telegram progress, blocker, milestone, or completion update, especially a voice message.
---

# Telegram Agent Updates

Use the scoped helper only inside a turn launched by Telegram Control. It sends
to the topic that owns the current turn and does not require a bot token.

Send a voice update:

```bash
/usr/bin/python3 /Users/shantam/telegram-control/agent_telegram.py voice --key completion <<'EOF'
The requested work is finished and verified.
EOF
```

Send a text update:

```bash
/usr/bin/python3 /Users/shantam/telegram-control/agent_telegram.py text --key milestone-tests <<'EOF'
The implementation is complete. I am running the final test suite now.
EOF
```

Use a stable, descriptive lowercase key for each distinct update. Repeating the
same key and content is idempotent.

Keep updates short and useful. Send them when the user requests them, at a
meaningful milestone, on a blocker that needs attention, or at completion. Do
not send routine narration. Never include secrets, tokens, private file
contents, or unreviewed sensitive data. Voice text is submitted to Microsoft
Edge TTS. The normal final response still belongs in the agent conversation.

---
name: telegram-voice-message
description: Send a Telegram voice message or voice note to the current topic from an active Telegram Control-managed Codex or Claude turn. Use implicitly whenever the user asks to send, reply with, or provide a voice message or voice note, including requests to send one at a milestone, on a blocker, or when work finishes. The user does not need to mention this skill by name.
---

# Telegram Voice Message

Send a concise voice note to the topic that owns the current managed turn:

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/agent_telegram.py voice --key completion <<'EOF'
The requested work is finished and verified.
EOF
```

Choose a stable lowercase key describing this distinct message. Repeating the
same key and content is idempotent.

Use this only inside a Telegram Control-managed turn. Keep the message short
and never include secrets or unreviewed sensitive data. Voice text is submitted
to Microsoft Edge TTS. Continue to provide the normal final response.

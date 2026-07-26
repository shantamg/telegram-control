---
name: telegram-text-update
description: Send a separate Telegram text progress or status update to the current topic from an active Telegram Control-managed Codex or Claude turn. Use implicitly whenever the user asks to be kept updated in Telegram or requests milestone, progress, blocker, or completion messages during a task. The user does not need to mention this skill by name.
---

# Telegram Text Update

Send a concise update to the topic that owns the current managed turn:

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/agent_telegram.py text --key milestone-tests <<'EOF'
The implementation is complete. I am running the final test suite now.
EOF
```

Choose a stable lowercase key describing this distinct update. Repeating the
same key and content is idempotent.

Use this only inside a Telegram Control-managed turn. Send updates when the
user requests them or when a material blocker needs attention. Avoid routine
narration and never include secrets or unreviewed sensitive data. Continue to
provide the normal final response.

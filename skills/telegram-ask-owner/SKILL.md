---
name: telegram-ask-owner
description: Ask the owner one question in Telegram with tappable button options from an active Telegram Control-managed Codex or Claude turn. Use implicitly whenever the user asks to be given choices, asks for a prompt with buttons, or when a decision between a few concrete alternatives must be made before work can continue. The user does not need to mention this skill by name.
---

# Telegram Ask Owner

Ask one bounded question in the topic that owns the current managed turn, with
2 to 5 button options:

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/agent_telegram.py ask \
  --key group-automation \
  --option "Deep link only" \
  --option "Full MTProto helper" \
  --option "Neither for now" <<'EOF'
How should new project groups be created?
EOF
```

A managed turn is one-shot, so the answer cannot return to the process that
asked. Each button queues a **new turn for this same agent** carrying the
question and the chosen option, exactly like Control's own clarification
buttons. So: ask, then finish your current turn with whatever you can say
without the answer. Do not wait or poll for the choice.

Keep option labels short (they are button text, 64 characters maximum) and make
them distinct — the label is the whole answer you will receive. Put the real
detail in the question. Choose a stable lowercase `--key`; reusing it is
idempotent, and tapping any option expires its siblings.

Use this only inside a Telegram Control-managed turn, and only when a real
decision is blocked. For an ordinary progress note use the text-update skill
instead.

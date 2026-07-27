---
name: telegram-create-topic
description: Create a new regular conversational Telegram topic and managed Codex or Claude session in the same bound group as the active Telegram Control turn, optionally queueing a first prompt so it starts working immediately. Use implicitly when the user asks to create or start another topic, thread, chat, conversation, or non-detached session, especially when work in the current topic surfaces a separate task that should continue independently. Do not use for report-only background or tmux workers; use telegram-detached-worker for those.
---

# Create a Conversational Telegram Topic

Use this only inside a Telegram Control-managed turn. It creates a fully
conversational topic in the active agent's bound private forum group. It never
copies the current provider conversation; the new topic has an independent,
lazy provider session.

Choose a short, descriptive topic name and a stable lowercase key. If the user
gave a task, context, or question for the new topic, turn it into a
self-contained first prompt and pass it on standard input:

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/agent_telegram.py \
  topic-create --key api-redesign --name "API redesign" <<'EOF'
Review the current API design, identify the new issue we just surfaced, and
propose a concrete implementation plan. Begin by inspecting the repository.
EOF
```

The helper queues that prompt as the new topic's first ordinary turn, so the
new session starts working immediately and remains steerable in its own topic.
If the user asked only for an empty ready-to-chat topic, provide empty standard
input:

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/agent_telegram.py \
  topic-create --key travel-planning --name "Travel planning" </dev/null
```

Omit provider settings to inherit the group's provider, model, and effort.
Pass only settings the user explicitly chose:

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/agent_telegram.py \
  topic-create --key release-audit --name "Release audit" \
  --provider claude --model opus --effort high <<'EOF'
Audit release readiness and report the first concrete blocker.
EOF
```

Do not guess a provider, model, or effort from subjective wording such as
"best"; ask the user when the request cannot safely inherit the group defaults.
Do not include secrets or irrelevant conversation history in the first prompt.
Do include the goal, constraints, important facts already established, and what
done means when the new topic cannot discover those itself.

The helper accepts no chat ID or topic ID. It verifies the active mailbox
lease, uses the agent's home group even if the current turn was reached through
a reply elsewhere, requires a bound private forum and Manage topics permission,
and rejects name or idempotency collisions. Report success only after it returns
the new topic details. This is not a detached worker: tell the user to continue
the conversation directly in the new topic.

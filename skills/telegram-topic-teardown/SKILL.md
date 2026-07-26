---
name: telegram-topic-teardown
description: Safely tear down, delete, remove, archive, or close the current managed Telegram topic and its bound Codex or Claude session through a durable confirmation button. Use implicitly whenever the user asks to remove the current topic, end its managed session, clean up its agent and database state, or avoid leaving stale routing after topic deletion. Do not use this for detached-worker report topics; use telegram-detached-worker to stop those workers.
---

# Managed Telegram Topic Teardown

Prefer the deterministic Telegram command:

```text
/teardown
```

It opens the confirmation card directly without invoking an LLM. If already
handling a natural-language teardown request inside an active managed turn,
request the same card with:

```bash
/usr/bin/python3 /Users/shantam/telegram-control/agent_telegram.py topic-teardown
```

Use either path only from the managed topic being removed. Neither accepts a
chat ID, topic ID, or agent ID.

Tell the user to use the Telegram confirmation card. Do not claim the topic was
removed merely because the card was posted.

Confirmation performs the destructive work through the controller: it rejects
an active agent turn, stops an interactive console, blocks while originating
detached workers still exist, archives the managed agent and session pointer,
revokes routes and buttons, and durably queues deletion of the Telegram topic.

Do not call `deleteForumTopic`, edit the controller database, kill tmux, or
delete the topic by another path. If the helper reports that this is not the
agent's home topic, return to that topic and request teardown there.

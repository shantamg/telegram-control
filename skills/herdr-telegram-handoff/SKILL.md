---
name: herdr-telegram-handoff
description: Open or remove the Telegram topic for the current running Herdr coding-agent conversation, and send it voice notes. Use from a Herdr pane when the user asks to continue in Telegram, send a Telegram voice note, or remove this session's linked Telegram topic.
---

# Continue this Herdr conversation in Telegram

Use this skill inside a running Herdr agent when the user asks to link or remove its Telegram topic or send a voice note.

## Open the linked Telegram topic

Choose a short topic name that describes the work and run:

```sh
/usr/bin/python3 "{{TELEGRAM_CONTROL_ROOT}}/herdr_telegram.py" link --name "Project conversation"
```

The command identifies the agent from the terminal environment, checks its process, and chooses the private Telegram Control group bound to the workspace. If it asks for a group, use `--group "Exact group name"` once the user has chosen the destination. Reusing the skill for the same running agent returns the existing topic. Report the URL from the command so the user can open the topic on their phone.

Keep working in the current conversation. The Telegram topic routes text and transcribed voice notes back to this pane. A prompt delivered from the topic contains a reply command for that message. For a Telegram prompt, use its reply command once with the actual answer on stdin. An incoming voice note already receives a spoken answer. For typed input requesting audio, append `:voice` to the reply ticket. This completes the turn. Then give the same answer in Herdr. Report any delivery error in Herdr.

## Send a voice note from Herdr

Use this standalone command for messages requested directly in Herdr. If there is no linked topic, open one first. Then pick a stable, lowercase key
for the requested message and pass the spoken text on stdin:

```sh
/usr/bin/python3 "{{TELEGRAM_CONTROL_ROOT}}/herdr_telegram.py" voice --key standalone-session-test <<'EOF'
This is a voice note from my Herdr session.
EOF
```

The helper returns the delivery state. Report queued or leased as pending and
sent as delivered. If a Telegram prompt supplied its own reply command, use
that command to answer the prompt.

## Remove the linked Telegram topic

When the user asks to remove the handoff, preview the linked topic:

```sh
/usr/bin/python3 "{{TELEGRAM_CONTROL_ROOT}}/herdr_telegram.py" teardown
```

Name the topic and ask the user in Herdr to confirm permanent deletion of the topic and its Telegram history. Mention that the current Herdr conversation keeps running. After the user's confirmation, pass the preview's `confirmation_id`:

```sh
/usr/bin/python3 "{{TELEGRAM_CONTROL_ROOT}}/herdr_telegram.py" teardown --confirm CONFIRMATION_ID
```

Report deletion when the command returns `sent`. If it returns `queued` or `leased`, say that removal is queued. The same confirmed command can check its status again. Report a `dead` result as failed Telegram delivery. If the preview or confirmation reports that the topic changed, get a fresh preview and confirmation.

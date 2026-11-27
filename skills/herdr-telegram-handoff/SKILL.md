---
name: herdr-telegram-handoff
description: Open or remove the Telegram topic for the current running Herdr coding-agent conversation. Use from a Herdr pane when the user asks to continue in Telegram, switch this session to Telegram voice notes, or remove this session's linked Telegram topic.
---

# Continue this Herdr conversation in Telegram

Use this skill inside a running local Herdr agent when the user asks to open or remove the Telegram topic for the current conversation.

## Open the linked Telegram topic

Choose a short topic name that describes the work and run:

```sh
/usr/bin/python3 "{{TELEGRAM_CONTROL_ROOT}}/herdr_telegram.py" link --name "Project conversation"
```

The command identifies the agent from the terminal environment, checks its process, and chooses the private Telegram Control group bound to the workspace. If it asks for a group, use `--group "Exact group name"` once the user has chosen the destination. Reusing the skill for the same running agent returns the existing topic. Report the URL from the command so the user can open the topic on their phone.

Keep working in the current conversation. The Telegram topic routes text and transcribed voice notes back to this pane. A prompt delivered from the topic contains a reply command for that message. When finishing that prompt, run its reply command with the final reply text on stdin, then give the same reply in Herdr. Report any delivery error in Herdr.

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

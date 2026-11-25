---
name: herdr-telegram-handoff
description: Continue the current running Herdr coding-agent conversation in Telegram. Use when the user asks from a Herdr pane to continue in Telegram, open a Telegram topic for this session, or switch this session to Telegram voice notes.
---

# Continue this Herdr conversation in Telegram

This skill is for a running agent inside a local Herdr terminal. Use it when the user asks to open a Telegram topic for the current conversation.

Choose a short topic name that describes the work and run:

```sh
/usr/bin/python3 "{{TELEGRAM_CONTROL_ROOT}}/herdr_telegram.py" link --name "Project conversation"
```

The command identifies the agent from the terminal environment, checks its process, and chooses the private Telegram Control group bound to the workspace. If it asks for a group, use `--group "Exact group name"` once the user has chosen the destination. Reusing the skill for the same running agent returns the existing topic. Report the URL from the command so the user can open the topic on their phone.

Keep working in the current conversation. The Telegram topic routes text and transcribed voice notes back to this pane. A prompt delivered from the topic contains a reply command for that message. When finishing that prompt, run its reply command with the final reply text on stdin, then give the same reply in Herdr. Report any delivery error in Herdr.

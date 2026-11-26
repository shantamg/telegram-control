---
name: herdr-session
description: Start or show a Claude Code or Codex session in Herdr, optionally with a Telegram topic. Use when the user requests a new Herdr session or wants an existing Herdr terminal shown in the app.
---

Use `herdr-spawn` when launching a new session that should appear in the Herdr app.

```sh
herdr-spawn --name review --agent codex --cwd /path/to/repo \
  --telegram "Review" -- "Review the changes and report the result."
```

- Use `--on <machine>` for a host listed by `hc machine ls`. Use `--no-report` to launch independently of the current Herdr terminal.
- The launcher reports the terminal address, whether Herdr delivered the initial task, and whether the app can show the terminal. If delivery fails, inspect the pane with `hc read <terminal>` before retrying.
- To show an existing terminal, run `herdr-show <terminal|machine/terminal>`. The helper can also place it in a named tab or space using `--tab` or `--space`.
- To link an existing Herdr session to Telegram, use `herdr-telegram-handoff` in that session. Use its reply command for prompts forwarded from Telegram.
- The host's Codex wrapper uses `--no-daemon` in Herdr so Telegram Control can identify the terminal from the tool process. Keep the Codex template command as `codex` to avoid passing duplicate flags.

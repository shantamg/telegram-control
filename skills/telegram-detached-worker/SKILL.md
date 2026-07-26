---
name: telegram-detached-worker
description: Start, report from, and tear down a detached tmux worker that keeps working after the current Telegram Control-managed turn ends, posting its progress into a Telegram topic of its own. Use implicitly whenever the user asks to run something in the background that must outlive this turn, to kick off a long job and be kept updated, to start a detached or tmux session for a task, or to stop or clean up one that is finished. The user does not need to mention this skill by name.
---

# Detached Telegram Worker

A managed turn is one-shot: this process is torn down as soon as you reply, and
anything you started in the background dies with it. Work that must keep going —
a long refactor, a review loop, a migration — belongs in a detached worker. The
tmux server is a daemon rather than a child of this turn, so the session
survives.

Each worker gets its own Telegram topic in the same group, and that topic is
**report-only**: the worker posts progress there, and the project's main topic
stays conversational. The user talks to you; you relay to the worker.

Use this only inside a Telegram Control-managed turn.

## Starting a worker

Pick a short lowercase slug for the worker (`rails-fix`, `inbox-triage`). It
names both the tmux session and the topic.

```bash
/usr/bin/python3 /Users/shantam/telegram-control/telegram_control.py \
  worker-start <name> --provider claude --model opus --effort high
```

- `--provider` takes `claude` or `codex`. Ask the user if they have not said.
- `--model` and `--effort` are optional; omit them to inherit local defaults.
- The command creates the topic (idempotent), starts the tmux session, and
  prints the session name and topic.

Then give the worker its task. **Write the brief to a file first** and point the
worker at it — briefs are long, and typing one through `send-keys` is fragile:

```bash
/usr/bin/python3 /Users/shantam/telegram-control/telegram_control.py \
  worker-brief <name> --file /absolute/path/to/BRIEF.md
```

A good brief states the goal, the constraints, what "done" looks like, and
explicitly tells the worker to report at milestones (see below). Include
anything the worker cannot discover for itself — it does not inherit your
conversation.

## Making the worker report

Tell the worker, in its brief, to run this whenever it reaches a milestone:

```bash
/usr/bin/python3 /Users/shantam/telegram-control/telegram_control.py \
  worker-report <name> --key <unique-slug> <<'EOF'
Spoken-style progress update.
EOF
```

Add `--text` for a written message instead of a voice note. Voice text is
converted to speech, so it must be plain spoken prose: no markdown, no file
paths, no line numbers, no code.

## Checking on a worker

```bash
/usr/bin/python3 /Users/shantam/telegram-control/telegram_control.py worker-status
```

Reports each worker's intended and observed state. `intended running` with
`observed stopped` means it died rather than being shut down.

To read what a worker is actually doing, capture its pane:

```bash
tmux capture-pane -p -t detached--<name> | tail -40
```

When polling a pane, do not grep for text you typed into it — your own prompt
is in the buffer and will match. Check for a real artifact instead.

## Tearing a worker down

When the user says the work is finished:

```bash
/usr/bin/python3 /Users/shantam/telegram-control/telegram_control.py \
  worker-stop <name> [--delete-topic]
```

**Ask the user whether to delete the topic** before passing `--delete-topic`.
Stopping always kills the session; deleting the topic also removes the history,
which they may want to keep. Without the flag the topic stays and the worker
row is removed.

## Rules

- One worker per task. Reuse the name to find an existing one rather than
  starting a second session for the same work.
- Never point a worker at a repository the user has not asked you to work in.
- The worker runs with the same permissions you do. Say so if the user asks for
  something destructive to run unattended.
- Do not tell the user to reply in the worker's topic — nothing there reaches
  the worker. Relay their instructions yourself.
- Report a worker as started only after the command succeeds.

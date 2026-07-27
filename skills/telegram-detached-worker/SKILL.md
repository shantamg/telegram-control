---
name: telegram-detached-worker
description: Start, report from, and tear down a detached tmux worker that keeps working after the current Telegram Control-managed turn ends, posting its progress into a Telegram topic of its own. Use implicitly whenever the user asks to run something in the background that must outlive this turn, to kick off a long job and be kept updated, to start a detached or tmux session for a task, or to stop or clean up one that is finished. The user does not need to mention this skill by name.
---

# Detached Telegram Worker

A managed turn is one-shot: this process is torn down as soon as you reply, and
anything you started in the background dies with it. Work that must keep going —
a long refactor, a review loop, a migration — belongs in a detached worker. The
tmux server is a daemon rather than a child of this turn, so the session
survives. Across a reboot, Telegram Control recreates tmux and resumes the exact
provider conversation.

Each worker gets its own Telegram topic in the same group, and that topic is
**report-only**: the worker posts progress there, and the project's main topic
stays conversational. The user talks to you; you relay to the worker.

Use this only inside a Telegram Control-managed turn.

## Starting a worker

Pick a short lowercase slug for the worker (`rails-fix`, `inbox-triage`). It
names both the tmux session and the topic.

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/telegram_control.py \
  worker-start <name> --provider claude --model opus --effort high
```

- `--provider` takes `claude` or `codex`. Ask the user if they have not said.
- `--model` and `--effort` are optional; omit them to inherit local defaults.
- The command creates the topic (idempotent), starts the tmux session, and
  prints the session name, topic, and durable recovery-file path.
- The harness gives the worker its full recovery-file contract once, as the
  launch prompt. Do not replace that contract with a scheduler: the provider
  continues to use its own native teamwork, wakeup, background, and scheduling
  features.

Then give the worker its task. **Write the brief to a file first** and point the
worker at it — briefs are long, and typing one through `send-keys` is fragile:

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/telegram_control.py \
  worker-brief <name> --file /absolute/path/to/BRIEF.md
```

A good brief states the goal, the constraints, what "done" looks like, and
explicitly tells the worker to report at milestones (see below). Include
anything the worker cannot discover for itself — it does not inherit your
conversation.

Each brief is prefixed with a two-line reminder of the worker's name and
recovery-file path, not the whole contract — the contract already arrived as the
launch prompt, and re-pasting it on every relay wasted context and told the
worker to wait for a brief that was already underneath it.

The contract instructs the worker to update its
durable `RECOVERY.md` whenever it creates, changes, completes, or cancels state
that matters after process loss. This includes goals, native scheduled tasks
and wakeups, background agents, monitors, exact restart commands, durable
artifacts and identifiers, verification steps, and idempotency warnings. On
recovery the same provider conversation reads that file, reactivates its own
native work, and explicitly confirms success or failure; the controller sends
the result through its durable Telegram outbox.

## Making the worker report

Tell the worker, in its brief, to run this whenever it reaches a milestone:

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/telegram_control.py \
  worker-report <name> --key <unique-slug> <<'EOF'
Spoken-style progress update.
EOF
```

Add `--text` for a written message instead of a voice note. Voice text is
converted to speech, so it must be plain spoken prose: no markdown, no file
paths, no line numbers, no code.

## Checking on a worker

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/telegram_control.py worker-status
```

Reports each worker's intended and observed state. `intended running` with
`observed stopped` means it died rather than being shut down. Recovery status
also shows whether the provider session is persisted, the recovery-file path,
and the last recovery error.

To read what a worker is actually doing, capture its pane:

```bash
tmux capture-pane -p -t detached--<name> | tail -40
```

When polling a pane, do not grep for text you typed into it — your own prompt
is in the buffer and will match. Check for a real artifact instead.

## Tearing a worker down

When the user says the work is finished:

```bash
/usr/bin/python3 {{TELEGRAM_CONTROL_ROOT}}/telegram_control.py \
  worker-stop <name> [--delete-topic]
```

**Ask the user whether to delete the topic** before passing `--delete-topic`.
Stopping always kills the session; deleting the topic also removes the history,
which they may want to keep. Without the flag the topic stays. In both cases,
the worker row and its exact controller-managed `RECOVERY.md` are removed; an
unexpected companion file is preserved rather than recursively deleted.

## Rules

- One worker per task. Reuse the name to find an existing one rather than
  starting a second session for the same work.
- Never point a worker at a repository the user has not asked you to work in.
- The worker runs with the same permissions you do. Say so if the user asks for
  something destructive to run unattended.
- Do not tell the user to reply in the worker's topic — nothing there reaches
  the worker. Relay their instructions yourself.
- Report a worker as started only after the command succeeds.

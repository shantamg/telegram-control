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
  prints the session name and topic.
- The launch prompt tells the worker only who it is and that it should keep
  using its own native scheduling, wakeup, loop, and background features. It is
  not asked to keep any recovery inventory.

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

The brief is delivered verbatim, with nothing prepended.

After the brief command succeeds, the detached worker owns the long-running
task. You may capture its pane once for an immediate sanity check, but **do not
wait or poll for a milestone from the managed parent turn**. In particular,
never run an `until tmux capture-pane ...` loop waiting for report text. The
worker's durable `worker-report` message is the confirmation; finish the parent
turn so it remains conversational and steerable.

For a later correction or follow-up, write the new instruction to another file
and deliver it with `worker-brief` again. Do not keep the parent turn open while
the detached worker processes it.

Recovery involves no agent at all. Resuming the exact provider session ID is
itself what restores the conversation and its scheduled work, so the controller
reopens the session with no prompt, checks the process came up, and posts that
the worker recovered. Nothing is asked of the worker and nothing is waited for.

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

## Typing into a pane yourself

`worker-brief` already does this correctly and verifies it landed, so prefer it.
When you must drive a pane directly — a confirmation dialog, a slash command,
another tmux session entirely — the submit is the part that goes wrong:

```bash
tmux send-keys -t <session> 'your text'      # 1. the text, alone
sleep 1
tmux send-keys -t <session> Enter            # 2. Enter, as its own call
sleep 1
tmux capture-pane -p -t <session> | tail -5  # 3. check it actually went
```

Three rules, each of which has already bitten:

- **Never put the text and `Enter` in one `send-keys`.** The Enter is swallowed
  and the text sits in the composer unsent.
- **Even sent separately, one Enter is not always enough.** Interactive
  providers process bracketed paste asynchronously, so an Enter arriving too
  early lands before the paste is submit-ready. A second bare `Enter` fixes it.
- **Verify, do not assume.** A submitted message moves out of the composer into
  the transcript and the pane shows the provider working — `esc to interrupt` or
  similar. Your text still sitting on the `❯` prompt line means it never went.
  Send another bare `Enter` and check again.

Clear a stale composer with `tmux send-keys -t <session> C-u` before typing, or
your text is appended to whatever was already there.

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
- A parent turn must never block on a detached worker's tmux pane. Report the
  worker as started after the start and brief commands succeed, then finish the
  parent turn.
- Report a worker as started only after the command succeeds.

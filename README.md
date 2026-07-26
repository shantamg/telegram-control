# Telegram Control

Run local Codex and Claude Code agents from your phone, over Telegram.

Send text, an attachment, or a voice note to your own Telegram bot; a durable
controller on your Mac saves attachments privately, transcribes voice locally,
routes the request to the right agent, runs the turn in the real working
directory, and streams the answer back with buttons you can tap to steer,
stop, or continue the conversation.

```
Telegram ──▶ collector ──▶ SQLite (durable queues) ──▶ router / agent workers
   ▲                                                          │
   └────────────── outbox sender (ordered, idempotent) ◀───────┘
```

Everything is local: one bot token in the macOS Keychain, one SQLite database,
one LaunchAgent, and the `codex` / `claude` CLIs you already use. There is no
server, no webhook, and no third-party service in the request path. Voice
transcription runs on-device; only optional spoken replies leave the machine.

## What it does

- **Text, attachments, and voice in; text and voice out.** Photos and arbitrary
  Telegram documents are saved to private durable paths that Codex and Claude
  can inspect. Voice notes are transcribed on-device with Handy's Parakeet V3
  model. Answers can be read aloud on demand via a **🔊 Listen** button.
- **One bot, many workspaces.** A private Telegram group per project, a topic
  per agent. Each topic is durably bound to a workspace directory and a
  persisted Codex or Claude session, so conversations survive restarts.
- **A conversational Control chat.** The main chat is an agent, not a command
  parser: it can search your directories (read-only, inside configured roots),
  inspect projects, dispatch work to project agents, and propose new agents.
  Every mutation is gated behind a one-time confirmation button.
- **Live turn control.** Each turn gets a progress card that streams the
  provider's user-facing output, a **⏹ Stop** button, and reply-to-steer: reply
  to the card and your text or voice becomes guidance for that exact turn.
  Stop also clears a locally orphaned turn immediately, so the next queued
  message is not held behind a dead worker's lease.
- **Durability as the design.** Every update, job, and outbound API call is
  committed to SQLite before it is acted on, with leases, retries, dead-letter
  states, and a single ordered delivery lock. Killing any process mid-turn
  loses nothing.
- **Recoverable detached workers** for jobs that must outlive a one-shot turn.
  Each keeps a durable recovery inventory, reports into its own Telegram
  topic, and resumes the exact Codex or Claude conversation after a reboot.
- **`/help` in Telegram**, a button-driven guide to the commands, agents,
  skills, and teardown flows.

## Requirements

| Requirement | Notes |
| --- | --- |
| macOS | LaunchAgent, Keychain, and `launchctl` are assumed throughout. |
| Python 3.9+ | Uses the system `/usr/bin/python3`. No third-party packages. |
| A Telegram bot | Create one with [@BotFather](https://t.me/BotFather). Enable **Threaded Mode** if you want topics in the private bot chat. |
| `codex` and/or `claude` CLI | At least one, already authenticated and working from your terminal. |
| [Handy](https://github.com/cjpais/Handy) with Parakeet V3 | Only needed for voice input. Expected at `/Applications/Handy.app/Contents/MacOS/handy` with models under `~/Library/Application Support/com.pais.handy/models/parakeet-tdt-0.6b-v3-int8`. |
| `ffmpeg` | Needed for voice in and out. Found at `/opt/homebrew/bin/ffmpeg`, `/usr/local/bin/ffmpeg`, or on `PATH`. |
| `edge-tts` | Optional, for spoken replies. Expected at `~/.local/bin/edge-tts`. |
| `tmux` | Optional, for the interactive console takeover and detached workers. |

Those last three binaries are looked up per use: an absolute override in
`config.json` (`handy_binary`, `ffmpeg_binary`, `edge_tts_binary`) wins, then
the documented locations above, then `PATH`. If one is missing entirely, voice
input or spoken replies fail with a clear message naming the path — text keeps
working.

## Setup

```sh
git clone <your-fork-url> telegram-control
cd telegram-control
```

**1. Pair the bot.** Double-click `SETUP.command`, or run it from the
repository root:

```sh
./SETUP.command
```

It asks BotFather for a token, verifies it, stores it in the macOS Keychain
under the service `telegram-bridge-bot-token`, then waits for you to send the
bot a message so it can record exactly which Telegram account and chat are
authorized. Nothing else is ever accepted. Press Control-C once the foreground
echo test replies to you.

**2. Create the durable database and check the prerequisites.**

```sh
./telegram_control.py init
./telegram_control.py doctor
```

**3. Install the background controller.** This replaces the Stage 0 listener
with the supervised collector, workers, and outbox sender, and starts them at
login:

```sh
./telegram_control.py install
./telegram_control.py status
```

**4. Install the agent-facing skills** so managed Codex and Claude turns can
send progress updates, voice notes, questions with buttons, group icons,
detached workers, and topic teardown:

```sh
./telegram_control.py install-skills
```

This copies each skill into `~/.agents/skills/` (which Codex reads directly)
and links it into `~/.claude/skills/`. Skill metadata is read when a provider
session starts, so restart an existing session before expecting a new skill to
appear.

**5. Add your first workspace.** Send `/newgroup` in the bot chat. It replies
with a link that adds the bot to a group you pick *and* requests the rights it
needs — Change group info, Delete messages, Manage topics — in the same
confirmation, so there is no separate promotion step. Telegram's Bot API
does not let a bot create a group or enable Topics, so those two steps are
yours: create a private group, turn on **Topics**, then tap the link.

Admin rights are not optional: Telegram's default Group Privacy hides ordinary
messages from non-admin bots.

Once it joins, the bot offers **Authorize forum** and then asks which folder the
group works in. Answer with a path or just a description:

```text
~/Software/my-project
the meet without fear repo in Software
```

Control resolves it and asks you to confirm **Bind forum workspace**. After
that, each new topic starts with the group's provider, model, and effort in one
tap — and whatever you already sent runs as that topic's first turn, so nothing
needs resending. Every topic opens with one message listing its agent, model,
effort, and context used, edited in place as those change, so it is always the
current status of that topic. Commands themselves come from Telegram's own menu:
type `/` in any chat or topic.

If you know the path up front, the first message can still do both at once:
*"Set up this group for /absolute/workspace/path using Claude"* offers a single
**Authorize and bind** button.

You can also work entirely in the private bot chat and let the Control agent
find directories for you: *"add a project called Lovely, the peter-app
subdirectory of the lovely repo in software inside my user directory"*.

## Everyday commands

Telegram commands (inside a chat or topic):

| Command | Effect |
| --- | --- |
| `/help` | Button-driven help browser. |
| `/status` | Inspect this Telegram surface; editable status card. |
| `/projects` | List enrolled workspaces. |
| `/newgroup` | Get the one-tap link that adds the bot to a new project group with the rights it needs. |
| `/agent` | Inspect or control this topic's agent: model, effort, session, console, context usage, pause/resume, new session, provider switch. |
| `/teardown` | Confirmation-gated removal of this managed topic and its session state. |

CLI (`./telegram_control.py <command>`, `--help` on any of them):

| Command | Effect |
| --- | --- |
| `init` / `doctor` / `status` | Create the database, check prerequisites, show queue state. |
| `install` / `request-restart` | Install or reload the LaunchAgent-backed controller. `request-restart` queues a reload the supervisor applies once nothing is leased. |
| `run` | Run everything in the foreground for debugging. |
| `collect`, `work`, `work-router`, `work-agents`, `send-outbox`, `maintain-topics` | Run one loop at a time; add `--once` to handle a single item. |
| `retry inbox\|router\|agent\|outbox` | Requeue dead-lettered items. |
| `enroll-project`, `provision-topic`, `register-agent` | Terminal-side workspace and topic wiring. |
| `console-open` / `console-status` / `console-close` | Explicit tmux takeover of a persisted agent session. |
| `worker-start` / `worker-status` / `worker-report` / `worker-stop` | Start, inspect, report from, and stop recoverable detached workers; stopping removes their managed recovery file. |
| `install-skills` | Install or refresh the repo-owned agent skills. |
| `sync-commands` | Publish the Telegram command menu from the help copy (`install` does this too). |

Prefer `request-restart` for reloads: it records the intention and the
supervisor applies it at the next idle moment, so no turn is aborted. `restart`
still exists for an immediate guarded restart, and `launchctl` should not be
used directly — macOS can turn a submitted reload job into a restart loop.

## Repository map

| Path | Responsibility |
| --- | --- |
| `telegram_control.py` | CLI and durable controller: collector, workers, outbox sender, supervisor, LaunchAgent install, console and worker commands. |
| `durable_store.py` | All SQLite persistence: schema, migrations, queues, leases, routes, callbacks, agents, sagas. The core of the system. |
| `on_message.py` | Per-turn message handler: authorization, commands, voice transcription, confirmations, card rendering. |
| `provider_adapters.py` | Provider-neutral turn execution for Codex and Claude, including streaming, usage, and interrupts. |
| `provider_defaults.py`, `codex_sessions.py`, `claude_sessions.py` | Read-only inspection of local provider config and persisted sessions. |
| `router_contract.py`, `router_eval.py` | The typed controller-tool vocabulary for the Control agent, and its repeatable eval gate. |
| `discovery.py` | Bounded, read-only filesystem discovery inside authorized roots. |
| `voice_responses.py` | Text-to-speech for Telegram voice replies. |
| `helper_paths.py` | Resolves the external helper binaries (ffmpeg, Handy, edge-tts) per machine. |
| `agent_telegram.py` | Helper agents call to post scoped text/voice updates from inside a turn. |
| `detached_worker.py`, `tmux_console.py` | tmux-backed detached workers and interactive session takeover. |
| `turn_guidance.py` | Guidance injected into every managed turn. |
| `telegram_help.py` | Copy for the in-Telegram `/help` browser. |
| `telegram_bridge.py` | Token/Keychain, config, pairing, and the raw Telegram API layer (plus the Stage 0 listener). |
| `skills/` | Repo-owned agent skills installed by `install-skills`. |
| `tests/` | Dependency-free unit and fault-injection tests. |

## Where to look for what

| Question | Go to |
| --- | --- |
| How do I use it from Telegram? | `/help` in Telegram — source of truth in `telegram_help.py`. |
| How does mechanism X actually behave, and what was verified? | [docs/IMPLEMENTATION_NOTES.md](docs/IMPLEMENTATION_NOTES.md) |
| Why is it built this way; what are the stages and acceptance gates? | [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) |
| What must I know before editing this repo? | [CLAUDE.md](CLAUDE.md) (`AGENTS.md` is a symlink to it) |
| What can agents do from inside a turn? | [skills/](skills/) |
| What is stored, and where? | `durable_store.py`, plus *Runtime state* below. |

## Tests

```sh
/usr/bin/python3 -m unittest discover -s tests -v
```

They are dependency-free and never call Telegram, read the Keychain, or launch
a provider. The router contract also has an offline eval gate, plus an optional
live run that uses isolated provider sessions:

```sh
/usr/bin/python3 router_eval.py
/usr/bin/python3 router_eval.py --live
```

## Runtime state

Deliberately outside Git:

- Bot token: macOS Keychain, service `telegram-bridge-bot-token`
- Incoming attachments:
  `~/Library/Application Support/telegram-bridge/attachments/inbox-<job-id>/`
- Pairing and configuration:
  `~/Library/Application Support/telegram-bridge/config.json`
- Durable database:
  `~/Library/Application Support/telegram-bridge/controller.sqlite3`
- LaunchAgent: `~/Library/LaunchAgents/local.telegram-bridge.plist`
- Logs, all in `~/Library/Logs/`: `telegram-control.log`,
  `telegram-bridge.log`, `telegram-bridge.error.log`

Optional `config.json` keys: `discovery_roots` bounds where the Control agent
may look for workspaces (defaults to your home directory); `handy_binary`,
`ffmpeg_binary`, and `edge_tts_binary` override the helper locations above with
absolute paths.

## Security boundary

- Incoming Telegram content is **data, never a shell command**.
- Only the single Telegram account confirmed during pairing is accepted, only
  in the paired private chat or in private forum groups with no public
  username. Everything else is discarded before its content reaches SQLite.
- Buttons carry opaque, short-lived, one-time action tokens — never commands,
  prompts, paths, or privileged payloads. The real action lives in SQLite and
  is revalidated against its chat, topic, user, and expiry on every tap.
- Filesystem access is bounded: agents run inside a confirmed workspace root
  enforced by symlink-resolved containment checks at proposal, confirmation,
  and every launch. Discovery is read-only and confined to the configured
  roots.
- Text quoted from replies is explicitly marked as data and cannot, by itself,
  authorize a dispatch — the user's own words must name the destination.
- The bot token never reaches a provider, a skill helper, or a process argument
  list.

Be clear-eyed about the flip side: managed agents run with permissive provider
settings by default (Codex `danger-full-access` / approval `never`, Claude
`bypassPermissions`), because an unattended turn cannot answer a permission
prompt. That is deliberate, and it means anyone who controls your paired
Telegram account can run code on your Mac. Set `provider_config.permission_mode`
to something stricter if that is not the trade you want.

## Status

Personal software, run daily by its author on one Mac. It is shareable and the
setup path above is real, but expect macOS-specific assumptions, hardcoded
helper paths, and single-host design (the delivery lock is a local `flock`).
Issues and forks welcome; do not expect it to be a product.

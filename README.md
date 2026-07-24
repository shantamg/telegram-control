# Telegram Control

Telegram Control is a phone-to-Mac control plane for local Codex and Claude
agents. The intended experience is:

1. Send text or a voice note in Telegram.
2. Transcribe voice locally with Handy's Parakeet V3 model.
3. Route the request to the main Codex controller or a project agent.
4. Receive text, a Telegram voice note, and contextual buttons that route
   follow-up actions back to the originating agent.

The repository currently contains the working Stage 0 proof of concept plus the
plan for building the durable controller. See [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md).

## What works now

- Computer → phone text messages.
- Phone → computer text messages that invoke a fixed local Python handler.
- Phone → computer voice notes transcribed locally with Parakeet V3.
- A macOS LaunchAgent that starts the listener at login and restarts it if it
  exits.
- Bot token storage in macOS Keychain.
- Authorization restricted to the Telegram private chat confirmed during
  pairing.

Stage 0 intentionally does not yet route messages into Codex, create agents,
send synthesized voice responses, provide buttons, or guarantee durable job
processing. Those are staged in the build plan.

## Stage 1 development path

The first durable-transport slice is now implemented alongside Stage 0. It
adds:

- a versioned SQLite store using WAL, foreign keys, full synchronization, and
  an integrity check;
- atomic update ingestion and polling-offset advancement;
- post-commit mirroring of the offset for a safe fallback to Stage 0;
- message and callback-query jobs with expiring leases, retry backoff, and a
  dead-letter state;
- an idempotent outbox for Telegram API calls;
- separate collector, inbox-worker, and outbox-sender loops;
- deterministic tests for duplicate ingestion, transaction rollback, worker
  death, stale leases, and durable handler replies.

Initialize a development database and inspect it:

```sh
/Users/shantam/telegram-control/telegram_control.py init
/Users/shantam/telegram-control/telegram_control.py status
/Users/shantam/telegram-control/telegram_control.py doctor
```

Run the durable transport in the foreground:

```sh
/Users/shantam/telegram-control/telegram_control.py run
```

After the live foreground smoke test passes, migrate the existing LaunchAgent
in place:

```sh
/Users/shantam/telegram-control/telegram_control.py install
```

The durable installer retains the existing LaunchAgent label so the Stage 0
poller is unloaded before Stage 1 starts. Do not run Stage 0 `listen` alongside
the Stage 1 collector because Telegram permits only one long poller for a bot.
Restore Stage 0 at any time with:

```sh
/Users/shantam/telegram-control/telegram_bridge.py install \
  --handler /Users/shantam/telegram-control/on_message.py
```

The active installation was migrated to the durable controller on July 23,
2026. A forced LaunchAgent restart recreated the supervisor and all three
workers, preserved the polling offset and queue state, and successfully
processed a new Telegram message after restart.

A full reboot/login test also passed on July 23, 2026. launchd started the
durable supervisor and all three workers automatically after login. A brief
post-boot network failure was retried automatically, and the first Telegram
message after reboot was committed, handled, and delivered without a queue
retry.

## Stage 2 callback checkpoint

Schema version 2 adds durable opaque callback actions. Text acknowledgments now
include an **Inspect transport** button whose compact `a:<token>` callback data
contains no command, path, prompt, or privileged payload. SQLite holds the
actual action and validates its chat, topic, authorized user, expiry, and
one-time state.

The live button test passed on July 23, 2026. The first tap resolved and
consumed the stored action and returned its routed response. A second tap
created a separate durable callback update but returned only “This button was
already used” without executing the action again.

Schema version 3 adds durable outbound-message return routes. After Telegram
confirms a `sendMessage` call, the outbox sender atomically binds the returned
Telegram message ID to its local target, chat, topic, policy, and expiry. Reply
messages resolve that binding from SQLite; none of those internal target
details appear in Telegram.

The live reply-routing test passed on July 23, 2026. The route was created,
survived a forced LaunchAgent restart, and then routed a Telegram reply back to
the controller. The routed response received its own return route.

For controlled debugging, each loop can run separately:

```sh
/Users/shantam/telegram-control/telegram_control.py collect
/Users/shantam/telegram-control/telegram_control.py work
/Users/shantam/telegram-control/telegram_control.py send-outbox
```

Use `--once` on any individual loop to handle at most one polling request or
queued item. Dead items are explicitly requeued with
`telegram_control.py retry inbox` or `telegram_control.py retry outbox`.

## Prerequisites

- macOS
- Telegram and a bot created with [@BotFather](https://t.me/BotFather)
- Handy with the Parakeet V3 model installed
- `ffmpeg` at `/opt/homebrew/bin/ffmpeg`

The current local transcription helper is:

```text
/Applications/Handy.app/Contents/MacOS/handy
```

The existing model files are expected under:

```text
~/Library/Application Support/com.pais.handy/models/parakeet-tdt-0.6b-v3-int8
```

## Set up or re-pair

Double-click `SETUP.command`, or run:

```sh
/Users/shantam/telegram-control/SETUP.command
```

The setup flow stores the Telegram bot token in Keychain under the service
`telegram-bridge-bot-token`. The token is never stored in this repository.

## Test the proof of concept

Computer to phone:

```sh
/Users/shantam/telegram-control/telegram_bridge.py send "Hello from my Mac"
```

Phone to computer, foreground:

```sh
/Users/shantam/telegram-control/telegram_bridge.py listen
```

Send the bot either text or a Telegram voice note. Text is acknowledged by the
Mac-side handler. Voice is downloaded into a private temporary directory,
converted to 16 kHz WAV, transcribed locally, returned as text, and deleted.

## Background listener

Install or update the per-user LaunchAgent:

```sh
/Users/shantam/telegram-control/telegram_bridge.py install \
  --handler /Users/shantam/telegram-control/on_message.py
```

Inspect it:

```sh
/Users/shantam/telegram-control/telegram_bridge.py status
```

Logs:

- `~/Library/Logs/telegram-bridge.log`
- `~/Library/Logs/telegram-bridge.error.log`

Remove only the listener, retaining pairing and the Keychain token:

```sh
/Users/shantam/telegram-control/telegram_bridge.py uninstall
```

## Current runtime state

Runtime state is deliberately outside Git:

- Token: macOS Keychain
- Pairing and handler configuration:
  `~/Library/Application Support/telegram-bridge/config.json`
- Telegram update offset:
  `~/Library/Application Support/telegram-bridge/offset`
- LaunchAgent:
  `~/Library/LaunchAgents/local.telegram-bridge.plist`

The legacy `telegram-bridge` names remain during Stage 0 so the working pairing
and Keychain entry continue to work. A later migration will rename runtime
components only if it can do so without losing queued messages.

## Tests

The Stage 0 tests are dependency-free:

```sh
/usr/bin/python3 -m unittest discover -s tests -v
```

They exercise token input sanitization and basic message metadata without
calling Telegram or reading the Keychain. Stage 0 uses the same Apple/Xcode
Python 3.9 interpreter as the LaunchAgent.

## Security boundary

Incoming Telegram content is data, never a shell command. The current handler
path is fixed in local configuration. Future buttons will contain opaque,
short-lived action IDs—not shell fragments, prompts, filesystem paths, or
privileged data. Every inbound event will be checked against the authorized
Telegram user/chat and its durable route record before an action is taken.

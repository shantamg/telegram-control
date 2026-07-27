# Telegram Control

Run Claude Code or Codex on your Mac from Telegram.

Telegram Control gives each project its own private Telegram group and each
conversation its own topic. Send text, files, photos, or optional voice notes;
the controller durably queues the work, runs the selected coding agent in the
bound local folder, and streams the answer back to Telegram.

The default experience is deliberately direct:

```text
private project group
└── Telegram topic ──▶ one Claude or Codex session in one local workspace
```

There is no required central routing agent. After a group is bound to a folder,
create a topic and start talking. An older conversational Control layer remains
available as an opt-in feature for people who want natural-language workspace
discovery and delegation.

> **Current support:** a single, local Mac. Linux servers, hosted deployment,
> containers, and multi-host operation are future work, not supported setup
> paths.

## What you need

| Capability | Required? | What happens without it |
| --- | --- | --- |
| macOS and Python 3.9+ | Yes | Installation stops with a specific readiness error. |
| A private Telegram bot | Yes | `SETUP.command` walks you through BotFather and pairing. |
| Claude Code **or** Codex CLI | Yes, one or both | Topics use whichever authenticated provider is available. A Claude-only installation does not need Codex. |
| Handy + Parakeet V3 + ffmpeg | No | Text and file messages work; local voice transcription is unavailable. |
| edge-tts + ffmpeg | No | Text replies work; the Listen button cannot create spoken replies. |
| tmux | No | Normal topic conversations work; console takeover and detached workers are unavailable. |
| Conversational Control agent | No, off by default | Groups bind by exact path with `/bind`; no Codex router is started. |

No Python packages need to be installed. Provider authentication is owned by
the Claude Code and Codex CLIs, just as it is in your terminal.

## Install on a local Mac

```sh
git clone https://github.com/shantamg/telegram-control.git
cd telegram-control
./SETUP.command
```

The setup assistant verifies the bot token, stores it in the macOS Keychain,
and waits for a message from the one Telegram account that will be authorized.
After the foreground echo test succeeds, press Control-C and run:

```sh
./telegram_control.py bootstrap
```

`bootstrap` runs the readiness checks, initializes the durable database,
installs the shared agent skills, publishes the Telegram command menu, and
installs the login LaunchAgent. Confirm the result with:

```sh
./telegram_control.py status
```

For the human checkpoints, troubleshooting, and uninstall details, follow the
[complete local-Mac setup guide](docs/getting-started/local-mac.md).

## Connect the first project

1. Send `/newgroup` to the bot's private chat.
2. Create a private Telegram group and enable **Topics**.
3. Use Telegram's **View as Topics** display mode, not **View as Messages**.
   This is a separate per-account display choice and makes each agent
   conversation feel like its own chat.
4. Tap the link from `/newgroup` to add the bot with the requested admin rights.
5. In the group, send `/bind` followed by an exact existing folder:

   ```text
   /bind ~/Software/my-project
   ```

6. Confirm the folder and provider. Then create a topic and send a message.
   The topic agent is created automatically and the message becomes its first
   turn.

See [Set up a Telegram project group](docs/getting-started/telegram-group.md)
for the exact Telegram settings and why each permission is needed.

## Everyday behavior

- One project group can contain many independent topic conversations.
- Topic sessions and queues survive controller restarts.
- `/projects` lists every connected workspace with its active topic and session
  counts, whether it came from the older project catalog or a bound group.
- `/agent` changes provider, model, effort, or session for the current topic.
- Replying to the progress card steers the active turn; **Stop** interrupts it.
- Attachments are saved to private local paths the agent can inspect.
- `/help` is the in-Telegram source of truth for commands and workflows.
- Agents can create more conversational topics when asked.
- Optional detached workers use tmux for work that must outlive one turn.

## Customize without changing the project

Behavioral preferences are layered so personal choices do not need to become
forks or change defaults for everyone:

```text
built-in defaults
  < private per-install config
  < workspace .telegram-control.json
  < workspace .telegram-control.local.json
```

You can choose the default provider, opt into a topic confirmation step, add
standing context or response-style guidance, and select compact, standard, or
detailed topic status text. The core safety and routing contract cannot be
replaced through these settings.

Start with [Customization and configuration](docs/guides/customization.md) and
inspect the resolved result with:

```sh
./telegram_control.py config show
```

## Documentation

The [documentation index](docs/README.md) separates first-time setup, user
guides, configuration reference, implementation details, and contributor
policy. In particular:

- [Local Mac setup](docs/getting-started/local-mac.md)
- [Telegram group and topic setup](docs/getting-started/telegram-group.md)
- [Providers and optional capabilities](docs/guides/providers-and-capabilities.md)
- [Customization](docs/guides/customization.md)
- [Security model](docs/reference/security.md)
- [GitHub maintainer setup](docs/contributing/maintainer-setup.md)
- [Repository architecture](docs/contributing/architecture.md)
- [Exact implementation notes](docs/IMPLEMENTATION_NOTES.md)
- [Original build plan](docs/BUILD_PLAN.md)

## Contributing

Issues, focused pull requests, and forks are welcome. Please read
[CONTRIBUTING.md](CONTRIBUTING.md) and [GOVERNANCE.md](GOVERNANCE.md) before
changing core behavior. User preferences should normally become explicit,
layered configuration rather than new universal defaults.

Run the dependency-free test suite with:

```sh
/usr/bin/python3 -m unittest discover -s tests -v
```

## Security

Only the Telegram account confirmed during pairing is accepted, only in its
paired private chat or explicitly authorized private forum groups. The bot
token stays in the macOS Keychain and is not passed to providers.

Managed agents intentionally run with permissive provider settings by default
because unattended turns cannot answer interactive permission prompts. Anyone
who controls the paired Telegram account can therefore cause code to run with
the agent's local permissions. Read the [security model](docs/reference/security.md)
before binding sensitive folders, and report vulnerabilities according to
[SECURITY.md](SECURITY.md).

## Project status

Telegram Control is personal software used daily by its maintainer. The local
Mac setup is the supported path, but it is not a hosted service or a polished
consumer product. The durable core is extensively tested; new installations
should still begin with a non-sensitive workspace.

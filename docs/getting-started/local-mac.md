# Install Telegram Control on a local Mac

This is the only supported deployment path today. The setup keeps the bot
token in the macOS Keychain, state in a local SQLite database, and the
controller in a per-user LaunchAgent that starts after graphical login.

## 1. Install one provider

Install and authenticate Claude Code, Codex, or both. Verify each provider you
intend to use by running it directly from Terminal before continuing. `doctor`
can run each CLI's `--version`, but provider login remains a separate manual
checkpoint.

Telegram Control does not broker provider credentials. If only `claude` is
available, new groups and topics use Claude and no Codex worker or central
router is required. If both are available, the `/bind` confirmation offers
both unless a configured default selects one.

## 2. Clone the repository

```sh
git clone https://github.com/shantamg/telegram-control.git
cd telegram-control
```

The repository is public, so cloning it does not require a GitHub account or
GitHub authentication.

## 3. Create and pair a Telegram bot

Create a bot with [@BotFather](https://t.me/BotFather), then run:

```sh
./SETUP.command
```

The assistant:

1. asks for the BotFather token;
2. verifies the token with Telegram;
3. stores it in Keychain service `telegram-bridge-bot-token`;
4. waits for you to message the bot;
5. records that Telegram account and private chat as the only owner surface;
6. starts a foreground echo test.

Send the bot one ordinary message and confirm that it echoes. Press Control-C
after the successful test. Do not paste the token into a repository file.

## 4. Check and install

Run the complete install:

```sh
./telegram_control.py bootstrap
```

The first part is a capability-aware doctor. It treats these as core failures:

- not running on macOS;
- Python older than 3.9;
- missing or invalid pairing/Keychain state;
- no runnable provider CLI;
- a selected default provider that is unavailable;
- enabling the optional Control agent without Codex.

Voice transcription, spoken replies, and tmux features are reported
separately as optional. Missing one does not prevent text, file, and topic
conversations from being installed.

`bootstrap` then creates or verifies the database and records the resolved
checkout as the install-time **Telegram Control** workspace. That durable seed
lets the first private forum group named **Telegram Control** offer this repo
for one-tap confirmation even when it was cloned somewhere other than the
example path. Bootstrap also copies the repo-owned skills to
`~/.agents/skills/`, links their Claude entries from `~/.claude/skills/`,
attempts to publish Telegram's command menu, writes the LaunchAgent, and starts
the controller. Codex discovers the shared skill directory directly. A network
failure while publishing the menu is a nonfatal warning; retry it with:

```sh
./telegram_control.py sync-commands
```

Inspect it at any time:

```sh
./telegram_control.py doctor
./telegram_control.py status
./telegram_control.py config show
```

## 5. Connect a project

Continue with [Set up a Telegram project group](telegram-group.md). Start with
a non-sensitive repository until you are comfortable with the permission
model.

## Logs and local state

| Data | Location |
| --- | --- |
| Bot token | macOS Keychain service `telegram-bridge-bot-token` |
| Pairing and install config | `~/Library/Application Support/telegram-bridge/config.json` |
| Durable database | `~/Library/Application Support/telegram-bridge/controller.sqlite3` |
| Attachments | `~/Library/Application Support/telegram-bridge/attachments/` |
| LaunchAgent | `~/Library/LaunchAgents/local.telegram-bridge.plist` |
| Logs | `~/Library/Logs/telegram-control.log` and `telegram-control.error.log` |

## Apply an update safely

Pull the desired revision and run its tests:

```sh
/usr/bin/python3 -m unittest discover -s tests -v
```

Then apply only the rollout steps relevant to that revision:

- If `telegram_help.COMMANDS` changed, run
  `./telegram_control.py sync-commands`; Telegram's menu is published state and
  is not derived from the handler at runtime.
- If a repo-owned skill under `skills/` changed, run
  `./telegram_control.py install-skills`.
- Handler changes are loaded by each new Telegram turn without a restart.
- If long-running worker code changed, request an idle restart:

```sh
./telegram_control.py request-restart --reason "Apply the updated controller"
```

The supervisor waits until no inbox, router, agent, or outbox work is leased.
Do not kill the workers or use `launchctl` directly while turns are active.

Schema migrations and new database payload variants create a mixed-version
window as soon as a fresh process writes or opens them. Follow any
revision-specific migration notes and queue the matching worker reload; do not
assume a schema change waits for a deploy step.

## Troubleshooting

Run `./telegram_control.py doctor` first. Its error should identify the missing
core capability, while its notices identify optional features.

Then inspect:

```sh
./telegram_control.py status
tail -n 100 ~/Library/Logs/telegram-control.error.log
```

If a provider is reported as unavailable, make sure the same macOS user can run
that provider from Terminal and complete its login flow. Telegram Control does
not refresh or store those credentials itself.

## Uninstall

Remove the loaded LaunchAgent and stop the background controller with:

```sh
./telegram_bridge.py uninstall
```

This intentionally keeps pairing, the Keychain token, SQLite state,
attachments, logs, and installed shared skills so reinstalling does not destroy
history. If you want a complete removal, first preserve anything you need,
remove the bot from its Telegram groups, and then delete the local-state paths
listed above, the installed `telegram-*` skills in `~/.agents/skills/` and their
matching Claude links, and the Keychain item named
`telegram-bridge-bot-token`. Those final deletions are manual and irreversible.

## Cloud deployment

A continuously running Mac or a future server installation can be useful, but
Linux/cloud deployment is intentionally deferred. The current implementation
assumes Keychain, LaunchAgents, graphical-login provider credentials, and
single-host file locking. Do not treat an EC2 deployment as a documented
installation variant yet.

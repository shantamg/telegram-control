# Telegram voice for remote Herdr sessions

Use one Telegram Control receiver per bot token. The receiver can run on a
Herdr host and connect to other Herdr hosts over restricted SSH commands.
Both directions still work when the phone's Herdr client disconnects.

On the receiver, install Telegram Control with its Git submodule at
`~/.local/share/telegram-control/app` and leave user services enabled after
logout with `loginctl enable-linger`. Install Python 3, ffmpeg with libopus,
Herdr Core, and the provider CLIs you plan to use. In
`~/.local/share/telegram-control/speech`, create a Python virtual environment
named `venv` with `sherpa-onnx` and `piper-tts`. Put the four files from
[the sherpa-onnx Parakeet V3 int8 release](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/offline-transducer/nemo-transducer-models.html)
(`encoder.int8.onnx`, `decoder.int8.onnx`, `joiner.int8.onnx` and `tokens.txt`)
alongside a Piper voice model named `voice.onnx` and its `voice.onnx.json`.
The configured voice speed applies to that model.

From the checkout, run `python3 telegram_bridge.py setup` to pair over hidden
input, then `python3 telegram_control.py install-skills`. If the bot was already
in service, coordinate its receiver cutover and preserve the old workspace
and session data before pairing. Pairing uses Telegram's update stream.
For a cutover that pauses Mac topics, set `"herdr_only_receiver": true` in
the receiver's private config. The receiver preserves authorized updates
outside linked Herdr topics in `telegram_updates` under `held_for_mac`,
without creating replacement sessions or running those updates. The old
controller's saved polling cursor should seed the Linux database after the
old receiver drains and stops.

Copy `deploy/telegram-control.service` to `~/.config/systemd/user/`, then run
`systemctl --user daemon-reload` and
`systemctl --user enable --now telegram-control.service` on the sole receiver.
Authorize a private forum with the bot as a topics admin and bind it to the
project's folder on the receiver. For a local agent in that folder, ask it to
continue in Telegram or to send a voice note. The helper returns the topic link.

## Add a Herdr host to the same bot

Install the application code and skills on the peer. It needs Herdr Core and
its provider CLI. The receiver holds the token and performs all audio work.

Create a dedicated SSH key per direction and use authenticated, pinned host
keys. On the peer, restrict the receiver key in `authorized_keys` with
`restrict,command="/usr/bin/python3 /absolute/path/herdr_telegram.py relay"`.
On the receiver, restrict the peer key with the same command plus
`--peer peer-name`. Use separate SSH aliases for these keys, and set
`IdentitiesOnly yes` and `StrictHostKeyChecking yes` for those aliases.

Put `{"hub":"receiver-ssh-alias"}` in the peer's
`~/.config/telegram-bridge/herdr-relay.json`. On the receiver, use this shape:

```json
{
  "peers": {
    "peer-name": {
      "ssh": "peer-ssh-alias",
      "script": "/absolute/peer/path/herdr_telegram.py",
      "group": "Name of a bound private Telegram forum"
    }
  }
}
```

The group is a default for links from that peer. Use an exact group name when
the peer serves multiple projects. Start a session on the peer in Herdr and ask
it to continue in Telegram. The receiver checks the peer's live terminal and
agent identity before linking, forwarding a turn, or accepting a reply.

## Roll out the same commands across the fleet

Install Herdr Core and a Codex CLI that supports `--no-daemon` on each host.
Clone this repository with its submodule, check out the same reviewed commit on
each host, and keep each release in its own directory. For example:

```sh
revision=REVIEWED_COMMIT_SHA
release="$HOME/.local/share/telegram-control/app-$revision"
mkdir -p "$(dirname "$release")"
git clone --recurse-submodules https://github.com/shantamg/telegram-control.git "$release"
git -C "$release" checkout --detach "$revision"
git -C "$release" submodule update --init
python3 "$release/deploy/herdr-host.py" install
python3 "$release/deploy/herdr-host.py" check
```

Use the same Python for both invocations. On a Mac, choose a Python 3.9+ that can
import `plistlib`. The installer uses its absolute path for the handoff
launcher. The current release can be checked from
`~/.local/share/telegram-control/app/deploy/herdr-host.py`.

The installer points the stable `app` link at the selected checkout. It installs
the shared **herdr-session** and **herdr-telegram-handoff** skills, their
relative Claude skill links, the Herdr Codex template, and the
`herdr-spawn`, `herdr-show`, and `herdr-telegram` commands. The Codex wrapper
uses `--no-daemon` only in a Herdr terminal. The launcher uses the existing
desktop bridge on a Mac that has one configured.

Keep the receiver's bot token, controller database, SSH relay identities, and
peer settings in place across releases. Peers need the restricted SSH relay
described above. Keep a single active receiver for the token. On that receiver,
verify the outbox and worker leases are clear before restarting the service
against the new `app` link. Verify the service is active, then test one existing
route. Re-running the installer from the previous checkout rolls the tools back.

### Updates on use

Push shared changes through the Telegram Control repository and merge them into
`main`. On each use, the installed Herdr session and Telegram launchers check
their checkout’s Git origin for the latest commit on `main`. Direct receiver
and relay invocations also check. If an update is available, the host downloads
a separate checkout with its pinned submodules, installs its skills and tools,
and runs the requested command from the new release. Simultaneous checks share
a host lock. A failed check prints a warning and continues with the active
installation. The Linux receiver requests a drained supervisor reload.

Install the fleet package once on each new host. Older installations need one
manual upgrade to a release with the updater. To inspect the active revision:

```sh
python3 ~/.local/share/telegram-control/app/deploy/herdr-host.py check --compare-branch main
```

Exit code 0 means the revision matches and all tools are present. Exit code 2
means the installed and published revisions differ.

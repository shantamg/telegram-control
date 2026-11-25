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

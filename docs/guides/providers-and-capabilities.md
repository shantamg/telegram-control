# Providers and optional capabilities

Telegram Control is capability-aware. The core needs at least one working
coding-agent CLI; the media and long-running-worker features are independent.

## Claude Code and Codex

| Installed providers | Default behavior |
| --- | --- |
| Claude only | Groups and topics use Claude. Direct mode works fully; no Codex router is launched. |
| Codex only | Groups and topics use Codex. |
| Both | A group binding offers both unless configuration selects one. |
| Neither | `doctor` and `bootstrap` stop before installation. |

Provider login remains the provider's responsibility. Run the CLI manually to
authenticate or refresh credentials. Telegram Control discovers the executable
and invokes the existing local session machinery; it does not copy provider
tokens into its config.

The optional conversational Control agent currently requires Codex. Enabling
it on a Claude-only machine makes `doctor` fail clearly rather than starting a
controller that cannot route.

Set an install or workspace default with
`telegram_control.defaults.provider`: `auto`, `claude`, or `codex`. `auto`
uses the providers actually present on the machine.

## Voice input

Voice-note transcription requires all three:

- [Handy](https://github.com/cjpais/Handy);
- its Parakeet V3 model;
- `ffmpeg`.

Transcription runs locally. If any component is absent, text, attachments, and
provider turns remain available and `doctor` reports voice input as optional
and unavailable.

## Spoken replies

The Listen action requires `edge-tts` and `ffmpeg`. It is optional and
independent from voice input. Spoken-reply synthesis uses the external
text-to-speech service used by `edge-tts`; ordinary text replies remain local
to the controller/provider path.

Send `/voice` in any authorized chat or topic to inspect the global
spoken-reply configuration. Voice and speed choices are staged first;
**Preview** generates a real Microsoft TTS sample without changing the
setting, **Confirm** applies it, and **Back** returns to the choices. The
selected configuration is shared by Listen actions, agent-authored voice
updates, and detached-worker voice reports.

## tmux

tmux is used only for:

- interactive console takeover of an existing provider session;
- detached workers that must survive a single Telegram turn.

Normal conversational topics do not require it.

## Binary overrides

The private install config may provide absolute paths:

```json
{
  "handy_binary": "/Applications/Handy.app/Contents/MacOS/handy",
  "ffmpeg_binary": "/opt/homebrew/bin/ffmpeg",
  "edge_tts_binary": "/Users/you/.local/bin/edge-tts"
}
```

Do not copy the example username. Use absolute paths from your own Mac.
Without overrides, Telegram Control checks its documented common locations and
then `PATH`.

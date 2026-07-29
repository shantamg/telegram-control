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

Set an install default at `telegram_control.defaults.provider` in the private
bridge config. Workspace files contain the settings object directly, so the
same field is `defaults.provider` there. Allowed values are `auto`, `claude`,
or `codex`; `auto` uses the providers actually present on the machine.

## Per-topic provider controls

Use `/agent` in a managed topic to change model or effort without discarding
the current conversation, pause or resume mailbox processing, start a fresh
session, resume a recent dormant session from the exact workspace, or switch
providers after confirmation. Switching providers starts a fresh conversation
but leaves the previous provider-owned session intact.

Choosing **Default** for model or effort follows the local CLI configuration:
top-level Codex values from `~/.codex/config.toml`, or Claude user/workspace
settings. The Telegram picker is the source of truth for the currently
supported explicit choices. Active turns can be steered by replying to their
progress card, and **Stop** uses the provider's native interrupt path.

## Local Ollama models through Codex

A Codex topic can use Ollama as its model backend while retaining Telegram
Control's Codex app-server integration. Start Ollama, open `/agent`, choose
**Change model / effort…**, select **Ollama local**, and then choose one of the
models currently returned by Ollama. Model names are discovered from the local
`/api/tags` endpoint; none are compiled into Telegram Control. Choosing a local
model applies it immediately because Ollama has no separate effort selection.

The selected configuration is stored per topic as `model_provider: "ollama"`
plus the chosen model name. Moving between OpenAI cloud and Ollama starts a
fresh provider session so conversation state cannot cross backends. Later turns
on the same local backend resume the persisted Codex thread. Local turns use a
read-only sandbox unless an explicit provider configuration overrides it.

Visible model text streams through the same editable Telegram progress card as
cloud Codex output. Small local models may follow instructions and tool
protocols much less reliably than hosted coding models; treat the backend as
experimental and inspect its work before granting write access.

Ollama defaults to `http://127.0.0.1:11434`. Set `OLLAMA_HOST` in the
controller's environment when the local service uses another HTTP endpoint.
The Codex CLI remains required because it supplies structured events, session
persistence, steering, interruption, and console integration around Ollama.

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

Send `/voice` in the paired private chat or an ordinary authorized project
topic to inspect the global spoken-reply configuration. Report-only detached
worker topics do not accept commands. Voice and speed choices are staged first;
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

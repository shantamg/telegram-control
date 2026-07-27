# Configuration reference

## Layers

| Layer | Path | Shape |
| --- | --- | --- |
| Built-in | `app_config.DEFAULT_SETTINGS` | Complete settings object |
| Per install | `~/Library/Application Support/telegram-bridge/config.json` | Settings nested under `telegram_control` |
| Shared workspace | `<workspace>/.telegram-control.json` | Settings object only |
| Private workspace | `<workspace>/.telegram-control.local.json` | Settings object only |

Objects merge recursively. A more specific value replaces the less specific
value at the same key.

## Fields

| Field | Default | Allowed values | Purpose |
| --- | --- | --- | --- |
| `control_agent.enabled` | `false` | Boolean | Opt into the conversational central Control layer. Requires Codex. |
| `topics.confirm_agent` | `false` | Boolean | Require provider confirmation before a new topic's first turn. |
| `defaults.provider` | `"auto"` | `auto`, `claude`, `codex` | Provider offered or selected for a newly bound group. |
| `prompts.preamble` | `""` | String, max 4,000 characters | Standing context appended to managed-turn guidance. |
| `prompts.preamble_file` | `""` | Path string | Read standing context from a file instead. |
| `prompts.response_style` | `""` | String, max 4,000 characters | Communication and response-style preference. |
| `prompts.response_style_file` | `""` | Path string | Read response style from a file instead. |
| `presentation.status_style` | `"standard"` | `compact`, `standard`, `detailed` | Amount of detail in a topic's editable intro/status message. |

Inline and file forms for the same prompt are mutually exclusive. Relative
file paths are permitted only with a workspace and must remain inside it.

## Runtime-managed spoken-reply setting

The global spoken-reply voice and speed are not fields in the layered settings
object. `/voice` stages, previews, and confirms that configuration, which is
then stored as validated JSON in SQLite's `controller_state`. It applies to
Listen actions, agent voice updates, and detached-worker voice reports. The
built-in fallback is Sonia at `+10%`.

## Machine-local bridge fields

The generated private `config.json` also contains pairing and machine identity
fields such as `chat_id`, `owner_user_id`, `bot_username`, and `handler_path`.
Do not hand-edit or copy those values between installations.

These optional top-level fields may be added to that same private file; they
are not valid in workspace settings:

| Field | Shape | Purpose |
| --- | --- | --- |
| `discovery_roots` | Array of absolute directory paths | Bounds exact-path validation and the optional Control agent's read-only workspace discovery. Defaults to the current user's home directory. |
| `handy_binary` | Absolute executable path | Overrides Handy discovery for local voice transcription. |
| `ffmpeg_binary` | Absolute executable path | Overrides `ffmpeg` discovery for voice input and spoken replies. |
| `edge_tts_binary` | Absolute executable path | Overrides `edge-tts` discovery for spoken replies. |

Binary resolution checks an explicit override first, then the documented common
locations, then `PATH`.

## Intentionally not configurable

The settings layer does not expose:

- Telegram authorization or allowed user IDs;
- workspace containment and symlink checks;
- callback token scoping, expiry, or one-time semantics;
- queue durability and lease behavior;
- provider lifecycle guidance;
- arbitrary commands, hooks, or executable paths inside a workspace;
- bot credentials.

Those are invariants, not style choices. A proposal to change one needs code,
tests, documentation, and explicit maintainer review.

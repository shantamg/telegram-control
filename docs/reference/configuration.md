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

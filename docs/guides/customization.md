# Customization without a permanent fork

Personal preferences should not require changes to shared Python code. Telegram
Control layers a small, validated settings surface over non-replaceable core
behavior.

## Precedence

Later layers override earlier ones:

1. built-in defaults;
2. the `telegram_control` object in the private install `config.json`;
3. `.telegram-control.json` in a bound workspace;
4. `.telegram-control.local.json` in that workspace.

Use the shared workspace file for conventions the repository's collaborators
should inherit. Use the local workspace file for personal preferences; the
filename is included in this repository's `.gitignore`, and projects adopting
the convention should ignore it too.

## Example

The repository's [`config.example.json`](../../config.example.json) shows every
layered behavioral setting. Machine-local discovery and binary paths are
documented separately in the
[configuration reference](../reference/configuration.md). For a workspace
file, copy only the object inside `telegram_control` because the workspace file
itself is the settings object:

```json
{
  "defaults": {
    "provider": "claude"
  },
  "prompts": {
    "preamble": "The maintainer values small, reviewable changes.",
    "response_style_file": "docs/agent-response-style.md"
  },
  "presentation": {
    "status_style": "compact"
  }
}
```

Save that as `.telegram-control.json`, or as
`.telegram-control.local.json` for an unshared preference.

For the private install file, merge the full wrapper into the existing paired
configuration:

```json
{
  "telegram_control": {
    "defaults": {
      "provider": "claude"
    }
  }
}
```

Never replace the complete generated `config.json`: it also contains pairing,
handler, and machine-specific data.

## Prompt customization

`prompts.preamble` adds standing project context.
`prompts.response_style` describes how the agent should communicate. Each has
a corresponding `_file` form for longer version-controlled text.

Set either the inline value or its file, never both. Relative prompt files are
resolved inside the bound workspace and may not escape it. Each resolved value
is limited to 4,000 characters.

These additions are appended after Telegram Control's core managed-turn
instructions. They cannot remove the security, routing, lifecycle, or scoped
Telegram capability contract.

## Presentation and workflow choices

- `presentation.status_style`: `compact`, `standard`, or `detailed`.
- `topics.confirm_agent`: `false` starts a new topic immediately; `true`
  restores a provider-selection confirmation step.
- `defaults.provider`: `auto`, `claude`, or `codex`.
- `control_agent.enabled`: `false` is direct mode; `true` enables the legacy
  conversational Control layer and requires Codex.

Inspect the effective install or workspace settings:

```sh
./telegram_control.py config show
./telegram_control.py config show --workspace ~/Software/my-project
```

Unknown keys, invalid values, conflicting prompt forms, unsafe file paths, and
missing prompt files fail with a precise configuration error.

See the [configuration reference](../reference/configuration.md) for every
field and default.

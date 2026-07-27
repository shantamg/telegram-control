# Security and permission model

Telegram Control turns messages from a phone into provider processes that can
modify files and run commands on a Mac. Convenience does not remove that trust
decision.

## Telegram boundary

- Pairing authorizes one Telegram account and its private bot chat.
- Project groups must be private, have no public username, and be explicitly
  confirmed by that owner.
- Updates outside those surfaces are discarded before their content reaches
  the durable queue.
- Buttons contain opaque, short-lived, one-time tokens. The privileged payload
  stays in SQLite and is checked against chat, topic, user, and expiry.
- The bot token lives in the macOS Keychain and is not included in provider
  environments or process arguments.

## Filesystem boundary

- Each group is bound to an explicitly confirmed local workspace.
- Paths are resolved and checked for containment at proposal, confirmation,
  and launch.
- Direct mode requires an exact existing path. Optional natural-language
  discovery is read-only and limited to configured discovery roots.
- Incoming attachments are stored under the controller's private application
  support directory.

## Provider permissions

Managed agents use permissive provider settings by default: Codex
`danger-full-access` with approval `never`, and Claude `bypassPermissions`.
That lets an unattended turn complete without waiting for a terminal approval,
but it also means the provider can act with the macOS user's access inside and,
depending on the provider, beyond the working directory.

Treat control of the paired Telegram account as control of the local agent.
Use a dedicated, non-administrator macOS account and bind only intended
workspaces if you need a smaller blast radius. Provider permission settings can
be made stricter in provider configuration, but unattended work may then pause
for approvals.

## Optional external paths

Voice transcription via Handy and Parakeet runs on-device. Spoken replies via
`edge-tts` use that tool's external text-to-speech service. Claude Code and
Codex communicate according to their own provider products and authentication.

## Public repository hygiene

The source repository is public, but repository visibility is unrelated to
runtime authorization. Every installation keeps its own private Keychain token,
pairing, database, attachments, and ignored local settings.

Continue to inspect commits and pull requests for tokens, private paths,
database files, logs, attachments, and personal content. If a secret ever
enters Git history, rotate it; deleting the current file is not sufficient.
GitHub visibility, rulesets, and collaborator permissions must be configured by
a repository owner because source files cannot enforce them.

Report a vulnerability privately using [SECURITY.md](../../SECURITY.md).

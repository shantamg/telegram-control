# Security policy

## Supported versions

Until formal releases exist, only the latest revision of the default branch is
supported with security fixes.

## Report a vulnerability privately

Do not open a public issue, discussion, or pull request containing exploit
details, credentials, private Telegram data, or a sensitive local path.

Use the repository's private
[GitHub security-advisory form](https://github.com/shantamg/telegram-control/security/advisories/new).
If private vulnerability reporting is not available, contact the repository
owner using the contact method listed on their GitHub profile and ask for a
private reporting channel before sharing details.

Include:

- the affected revision and macOS version;
- required Telegram/provider conditions;
- reproduction steps or a minimal proof of concept;
- likely impact;
- any suggested mitigation;
- whether the issue is already public or actively exploited.

The maintainer will acknowledge a usable report, assess scope, and coordinate
disclosure. Response times are best effort because this is an independently
maintained project.

## High-risk areas

Reports are especially valuable for:

- bypassing the paired-user or private-group authorization boundary;
- escaping a confirmed workspace or discovery root;
- reusing, forging, or redirecting callback actions;
- exposing Keychain tokens or scoped Telegram credentials to providers;
- cross-topic or cross-agent routing;
- command injection through Telegram content or filenames;
- unsafe mixed-version database behavior;
- unintended permission broadening.

The intentionally permissive permissions granted to managed Claude/Codex
processes are documented behavior, not by themselves a vulnerability. A bypass
of the configured provider permission mode or Telegram/workspace boundary is.

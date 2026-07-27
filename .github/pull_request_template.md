## What changed

<!-- Describe the user-visible and implementation changes. -->

## Why

<!-- Link the issue or explain the concrete need. -->

## Compatibility and risk

<!-- Include persisted-state, mixed-version, security, permission, and default-behavior effects. -->

## Verification

<!-- List exact automated and manual checks. -->

- [ ] `/usr/bin/python3 -m unittest discover -s tests -v`
- [ ] `git diff --check`
- [ ] User-facing Telegram changes update `telegram_help.py`.
- [ ] Setup/configuration/architecture changes update the focused docs.
- [ ] Exact behavior and verification are recorded in `docs/IMPLEMENTATION_NOTES.md`.
- [ ] No secrets, private Telegram data, personal paths, logs, attachments, or databases are included.

# Contributing to Telegram Control

Thank you for helping improve Telegram Control. The project welcomes bug
reports, focused proposals, documentation, tests, and pull requests that
preserve its security and durable-routing model.

## Before proposing a change

- Read the [README](README.md), [governance policy](GOVERNANCE.md), and
  [architecture guide](docs/contributing/architecture.md).
- Search existing issues and pull requests.
- For a significant new behavior or changed default, open an issue first.
- Never open a public issue for a vulnerability; use [SECURITY.md](SECURITY.md).

Preferences such as response style, standing context, status copy, and provider
defaults should normally be implemented as validated configuration with a
stable default. A personal preference is not, on its own, a reason to change
everyone's behavior.

## Development setup

The supported development environment is macOS with Python 3.9 or newer. The
test suite has no third-party Python dependencies:

```sh
git clone https://github.com/shantamg/telegram-control.git
cd telegram-control
/usr/bin/python3 -m unittest discover -s tests -v
```

You do not need a Telegram token or provider login to run tests. Pairing is only
needed for manual integration testing.

External contributors should fork the repository, create a branch in their
fork, and open a pull request. Collaborators with write access should still use
a branch. Do not push directly to `main`.

## Pull-request expectations

A good pull request:

- addresses one coherent problem;
- explains the user-visible effect and compatibility impact;
- includes tests proportional to the change;
- updates `telegram_help.py` for any user-facing Telegram behavior;
- updates README or focused docs when setup, commands, configuration, or
  architecture changes;
- records exact behavior and verification in `docs/IMPLEMENTATION_NOTES.md`;
- avoids unrelated formatting or cleanup;
- contains no bot tokens, provider credentials, private paths, logs,
  attachments, databases, or personal Telegram data.

Every Telegram command must be defined in `telegram_help.COMMANDS`, handled
through `addressed_command`, and covered by the command-registration tests.

## Verification

Before requesting review:

```sh
/usr/bin/python3 -m unittest discover -s tests -v
git diff --check
```

If you changed a live installation, use the repository's queued idle-restart
mechanism. Never kill active controller workers to apply a patch.

## Review and merge

Passing CI is necessary but does not guarantee merge. Maintainers evaluate
security, durability, product direction, default behavior, documentation, and
maintenance cost. They may ask that an opinionated behavior become an optional
setting, or that a large change be split.

By submitting a contribution, you agree that it is licensed under this
repository's [Apache License 2.0](LICENSE).

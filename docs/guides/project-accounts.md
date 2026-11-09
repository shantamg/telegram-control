# Project accounts

A company profile pairs a Claude Code login with a Codex CLI login. Terminal
launches and Telegram Control look for the same `.ai-account.local` file in
the working directory or its ancestors through the Git root. A linked worktree
can inherit the primary checkout's root binding. The file stores the company
label, and the CLI adds the filename to the repository's local Git exclude.

## Set up the terminal wrapper

The account switcher is its own project,
[ai-accounts](https://github.com/shantamg/ai-accounts), and Telegram Control
uses it through the `vendor/ai-accounts` submodule. Install the `ai-accounts`
command and the `c` and `codex` aliases by following its README. In short:

```sh
git clone https://github.com/shantamg/ai-accounts.git ~/ai-accounts
~/ai-accounts/install.sh --aliases
```

That README also covers creating profiles, signing in (the `c` and `codex`
picker can add an account and guide you through sign-in), and binding
projects. Telegram Control reads the same profiles and `.ai-account.local`
bindings, so set them up in Terminal first.

To move Telegram Control to a newer ai-accounts, update the submodule:

```sh
git submodule update --remote vendor/ai-accounts
```

Commit the new submodule pointer, then queue a restart with
`./telegram_control.py request-restart`.

## Use the binding in Telegram

Bind the Telegram group to its workspace as usual. A managed topic uses its
working directory to select the account and to scope the **Resume session**
picker. Console takeover, detached-worker startup, and detached-worker recovery
use the workspace binding too. Unbound Telegram workspaces and the optional
Control router use the controller's default launch behavior.

If you need to change the binding, finish active turns and stop the workspace's
consoles and detached workers first. Run `ai-accounts bind Work --replace` from
the directory whose binding you want to replace. Use **Start fresh session**
in `/agent` if the topic's previous session lives in the old profile, or use
the picker to choose a session from the new profile.

The wrapper clears inherited provider credential overrides such as
`OPENAI_API_KEY` and `ANTHROPIC_API_KEY` for a bound launch. Use native provider
settings that select the intended sign-in in each profile.

# Project accounts

A company profile pairs a Claude Code login with a Codex CLI login. Terminal
launches and Telegram Control look for the same `.ai-account.local` file in
the working directory or its ancestors through the Git root. A linked worktree
can inherit the primary checkout's root binding. The file stores the company
label, and the CLI adds the filename to the repository's local Git exclude.

## Set up the terminal wrapper

Put a link to the script in a directory on your `PATH`. Run this from the checkout.

```sh
mkdir -p ~/bin
ln -s "$PWD/ai_accounts.py" ~/bin/ai-accounts
```

Add the aliases to your interactive shell configuration, such as `~/.zshrc`.

```sh
alias codex='ai-accounts run codex --'
alias c='ai-accounts run claude --'
```

The wrapper is also callable as `ai-accounts run claude --` or
`ai-accounts run codex --` without aliases. It passes the rest of the command
line to the provider.

## Configure and sign in

Choose company labels that fit your setup. In this example, Personal uses the
two existing default CLI sign-ins and Work gets two new account directories.

```sh
ai-accounts profile Personal --claude-dir default --codex-dir default
ai-accounts profile Work
ai-accounts login Work claude
ai-accounts login Work codex
ai-accounts status
```

You can also specify existing private profile directories with
`--claude-dir /absolute/path` or `--codex-dir /absolute/path`. Claude Code
uses the directory to scope its macOS Keychain entry. For a custom Codex
directory, the wrapper selects file-backed credentials in that home. Default
profiles use the CLI's existing credential storage setting. Profile mappings
are private files under `~/.config/ai-accounts/profiles/`.

In a project directory, launch `codex` or `c` and select a company signed in to
the requested provider. The wrapper saves the binding at the Git root or in the
current directory for a non-Git project. For an explicit binding or a directory
override within a repository, run `ai-accounts bind Work` in that directory.
Run `ai-accounts show` to inspect the effective company. A shell without an
interactive terminal requires an existing binding.

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

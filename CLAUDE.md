# Repository Instructions

Telegram Control runs local Codex and Claude agents from Telegram. Start with
[README.md](README.md) for what it is and how it is set up.

## Where to look things up

| If you need | Read |
| --- | --- |
| Orientation, setup, commands, repository map | [README.md](README.md) |
| Exact behavior, invariants, and failure semantics of a mechanism | [docs/IMPLEMENTATION_NOTES.md](docs/IMPLEMENTATION_NOTES.md) |
| Original design, principles, stages, acceptance gates | [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md) |
| What the user sees in Telegram (`/help` copy) | [telegram_help.py](telegram_help.py) |
| What agents may do from inside a turn | [skills/](skills/) |
| Persistence, schema, migrations, queues, routes | `durable_store.py` |
| Per-turn handling, commands, cards, confirmations | `on_message.py` |
| Turn execution for Codex and Claude | `provider_adapters.py` |
| The Control agent's tool vocabulary and its eval gate | `router_contract.py`, `router_eval.py` |

`AGENTS.md` is a symlink to this file; edit this one.

## Keep the docs in step with the code

When you change or add a user-facing feature, update the in-Telegram help copy
in `telegram_help.py` in the same change, and the README if the setup path,
commands, or repository map moved. Record new behavior and what you verified in
`docs/IMPLEMENTATION_NOTES.md`.

Nothing here should hardcode a personal path, username, or bot name: this repo
is meant to be usable by someone else's setup. Resolve paths from the script
location, `Path.home()`, or config.

After completing changes in this repository, always commit the completed work
and push the commit to the configured remote before reporting completion.

## This repository is live while you edit it

The controller is running the whole time you work on it, and other agents are
usually mid-turn in other topics. Assume you are changing a system under load,
not a checkout.

Two consequences:

**Never restart the service to "apply" your change.** `install`, `restart`, and
killing the background processes all abort whatever turns are in flight —
including other agents' work, which simply dies mid-task. Before anything that
restarts the controller, check for live turns and wait until it is quiet:

```bash
sqlite3 -readonly "$HOME/Library/Application Support/telegram-bridge/controller.sqlite3" \
  "SELECT mailbox_id, agent_id, state FROM agent_mailbox WHERE state IN ('queued','leased');"
```

Committing and pushing is safe. Restarting is not, and is rarely necessary: the
message handler runs as a fresh process per turn, so handler-side changes take
effect on their own.

**A schema bump goes live the moment any new process opens the database.** It
does not wait for a deploy. `SCHEMA_VERSION` is compared on every store open,
and `_run_migrations` raises `IncompatibleSchemaError` when the database is
newer than the code — so the long-running `collect` / `work` / `work-agents`
processes, which still hold the previous version in memory, fail if they open a
store after your migration lands. This has already caused real damage once:
`telegram-control.log` records router turns abandoned four times over with
"Database schema 17 is newer than supported schema 16".

When adding a migration, know that you are changing shared state for every
agent on the machine, and that the gap between the migration landing and the
daemons picking up matching code is a genuinely mixed-version period. Prefer
migrations older code can tolerate — a new table or a nullable column is safe
to ignore, whereas renaming or dropping something the running version still
reads is not. Say plainly in your report that a migration has been applied and
that the daemons run older code until their next restart.

**The schema is not the only mixed-version surface.** Anything a fresh handler
process writes into the database and a long-running daemon later interprets is
one too: outbox `card_json` kinds, route shapes, payload fields. A new
`card_json` kind did exactly this on July 26, 2026 — the running sender was
older, could not interpret it, raised, and the supervisor restarted the whole
controller under live turns. `complete_outbox` now ignores card kinds it does
not recognize instead of raising, but the rule stands: when you add a variant
the daemons must read, make the unknown case a no-op for older code, and expect
the first row to be handled by the previous version.

Editing a live handler file is itself a rollout. `on_message.py` is re-read per
turn, so a half-saved refactor is a syntax error for whoever messages the bot
during that window. Prefer edits that keep the file parseable at every step,
and check `~/Library/Logs/telegram-control.error.log` afterwards.

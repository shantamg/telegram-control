# Repository architecture

Telegram Control separates transport, durable state, per-turn handling, and
provider execution so each can fail and retry without losing accepted work.

```text
Telegram
  │
  ▼
collector ──▶ durable SQLite queues ──▶ agent workers ──▶ Claude or Codex
  ▲                    │                       │
  └──── outbox sender ◀┴───────────────────────┘
```

The optional Control router is another durable worker between inbox and agent
queues. It is omitted from the default supervisor topology.

## Main modules

| Path | Responsibility |
| --- | --- |
| `telegram_control.py` | CLI, readiness, worker loops, supervisor, LaunchAgent installation, console and detached-worker commands |
| `telegram_bridge.py` | Telegram API, Keychain token, pairing, private config, collection |
| `durable_store.py` | Schema, migrations, queues, leases, routes, callbacks, agents, topic state |
| `on_message.py` | Fresh per-update handler, authorization, commands, direct binding, cards, confirmations |
| `provider_adapters.py` | Provider-neutral Claude and Codex execution, streaming, usage, interrupts |
| `app_config.py` | Validated install/workspace customization layers |
| `turn_guidance.py` | Non-replaceable managed-turn contract plus user customization |
| `telegram_help.py` | Single source for Telegram's command menu and `/help` copy |
| `router_contract.py`, `router_eval.py` | Optional Control agent tools and eval gate |
| `discovery.py` | Bounded read-only workspace discovery |
| `helper_paths.py`, `voice_responses.py` | Optional media capability discovery and spoken replies |
| `agent_telegram.py`, `skills/` | Scoped helpers available inside managed turns |
| `detached_worker.py`, `tmux_console.py` | Optional tmux-backed long-lived work and console takeover |
| `tests/` | Dependency-free unit, integration, and fault-injection tests |

## Durable routing model

A private forum group is bound to one workspace and default provider. Each
Telegram topic is bound to one managed agent and persisted provider session.
Messages route by Telegram chat and thread identity, not by interpreting their
contents. New direct-mode topics provision deterministically.

The collector persists accepted updates before acknowledging them. Workers
claim jobs with leases, and outbound Telegram calls are themselves durable and
ordered. See [implementation notes](../IMPLEMENTATION_NOTES.md) for exact
failure semantics and migration history.

## Live-development constraints

This repository is often the checkout used by a running controller:

- handler code is imported by a fresh process for each turn;
- worker code remains loaded until an idle restart;
- schema changes create a mixed-version window immediately;
- database payload variants may be written by new handlers and read by old
  workers.

Read [`CLAUDE.md`](../../CLAUDE.md) before editing. Never restart the live
service directly to apply a change; queue `request-restart` so leased work can
finish.

## Where preferences belong

Core routing, authorization, safety, and durability stay in code with tests.
Provider choice, prompt context, response style, topic confirmation, and status
presentation belong in the validated configuration layer. Read
[GOVERNANCE.md](../../GOVERNANCE.md) before proposing a new universal default.

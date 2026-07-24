# Telegram Control staged build plan

## Outcome

Telegram becomes a durable mobile interface for a hierarchy of local agents:

```text
Telegram text / voice / buttons
                 │
                 ▼
       durable local controller
                 │
                 ▼
        main Codex router
          ┌──────┴──────┐
          ▼             ▼
   project agent   project agent
          │
          ▼
    task workers
```

Any managed agent can send a response containing:

- text;
- a synthesized Telegram voice note;
- inline buttons;
- a return route that sends replies and button callbacks to that same agent.

The agent is a durable logical record. A Codex/Claude session and a tmux session
are replaceable execution details. Destroying tmux must not destroy the route,
queue, project identity, or conversation resume identifier.

## Principles

1. **Telegram is an interface, not the source of truth.** Once received, every
   event and job is recorded locally before Telegram's update offset advances.
2. **The LLM decides; scripts enforce.** Codex handles ambiguous routing and
   orchestration. Small programs handle persistence, validation, naming,
   locking, retries, and process lifecycle.
3. **Deterministic actions do not need an LLM.** `status`, `inspect`, `cancel`,
   menu navigation, and an unambiguous route go directly through the controller.
4. **One serialized queue per agent.** Two messages must not concurrently mutate
   the same Codex/Claude conversation.
5. **At-least-once internally, idempotent at boundaries.** Crashed jobs are
   retried. Every state-changing operation has a durable operation ID and checks
   whether it already happened.
6. **No message content becomes shell syntax.** Processes receive structured
   arguments/stdin. Paths and providers must come from validated registry data.
7. **Context is reconstructed, not merely remembered.** The main router receives
   compact durable state on every turn and can be rotated without losing system
   knowledge.
8. **Build the smallest useful layer, prove it, then add autonomy.**

## Current proof of concept (Stage 0)

### Proven

- Telegram Bot API connectivity in both directions.
- Private-chat pairing and user/chat allowlisting.
- macOS Keychain token storage.
- A LaunchAgent restarts the long-poll listener.
- Local voice transcription using Handy and Parakeet V3.
- Temporary audio cleanup.

### Known limitations

- The listener runs handlers synchronously.
- It saves the next Telegram offset even when a handler fails; that can lose a
  job.
- It has no durable inbox, outbox, retry counter, lease, dead-letter queue, or
  agent registry.
- Replies always go to the one paired private chat and do not preserve a topic
  or agent route.
- It accepts only `message` updates, not button `callback_query` updates.
- It sends only text.
- `launchd` starts the listener after the user logs in. It cannot receive while
  the Mac is asleep or powered off.

Stage 0 remains runnable while Stage 1 is developed.

## Stable identity and naming protocol

### Logical agent identity

Every agent receives an immutable random `agent_id`. Display names and tmux
names are derived labels; they are not database keys.

Each record includes:

- `agent_id`
- `parent_agent_id`
- `role`: `main`, `project`, or `worker`
- `slug`
- `provider`: `codex` or `claude`
- `project_path`
- provider conversation/session ID
- Telegram chat/topic binding
- lifecycle state
- created/updated timestamps

### Hierarchical names

The root controller is:

```text
tc--root
```

A child appends its own slug to its parent's complete name:

```text
tc--root--reservations
tc--root--reservations--stripe-debug
tc--root--reservations--stripe-debug--tests
```

Rules:

- lowercase ASCII letters, digits, and single hyphens inside a slug;
- `--` is reserved as the hierarchy delimiter;
- the controller, not the LLM, calculates and validates the final name;
- only a registered parent can create a child;
- sibling names must be unique while active;
- depth and total length have configurable limits;
- tmux sessions created outside this registry are never adopted implicitly;
- the main router normally talks to project agents, not their workers;
- direct worker routing happens only through an explicit topic binding,
  button route, or user instruction.

If a tmux view is recreated, it uses the same derived name when available. A
collision with an unmanaged tmux session is reported; it is never killed or
reused automatically.

## Telegram interaction model

### Conversation surfaces

Begin with the paired private bot chat. Prefer Telegram's bot private-chat
topics when enabled for the bot. If that feature is unavailable or awkward,
use one private forum supergroup with topics. The persistence model always
stores both `chat_id` and optional `message_thread_id`, so the transport can
support either without redesign.

Suggested surfaces:

- `Control` — main Codex router and global commands.
- one topic per project agent;
- optional task topics bound directly to long-lived workers;
- transient buttons for one-off routes and approvals.

### Return routes

Every outbound agent response records a durable route:

```text
route_id -> agent_id + chat_id + topic_id + policy + expiry
```

Replies can route using the replied-to Telegram message ID. Inline buttons use
only a compact opaque callback token, for example `r:6J4K2P`. The database maps
that token to the full action.

Callback data must never contain:

- a shell command;
- a filesystem path;
- a raw user prompt;
- a tmux target;
- a provider session ID;
- a privileged action payload.

For each callback the controller validates user ID, chat ID, topic, expiry,
current agent state, and whether a one-time action has already been consumed.
Destructive or privilege-changing operations require a second confirmation.

### Example agent response

```text
reservations · stripe-debug

I found the failing webhook signature check. I can patch it and run the tests.

[Approve patch] [Ask a question]
[Inspect]       [Voice summary]
[Stop worker]
```

All buttons return to `stripe-debug` unless the action explicitly targets its
parent project agent.

## Durable data model

Use Python's built-in SQLite support initially, with WAL mode, foreign keys,
busy timeout, and transactions.

Proposed tables:

- `telegram_updates`
  - unique `update_id`, raw JSON, received time, ingest state
- `inbox_jobs`
  - job ID, update ID, kind, normalized input, route, state, attempts,
    `available_at`, lease owner/expiry, last error
- `outbox_messages`
  - message ID, destination, text/voice/buttons payload, state, attempts,
    `available_at`, last error, Telegram result IDs
- `agents`
  - logical hierarchy, provider, project, provider session ID, lifecycle state
- `agent_mailbox`
  - serialized user/controller inputs per agent
- `routes`
  - Telegram message/reply/callback/topic bindings to agents and actions
- `operations`
  - idempotency key, requested action, result
- `router_memory`
  - compact durable decisions, aliases, project bindings, and summaries
- `events`
  - append-only audit records without secrets

State transitions use compare-and-swap updates inside transactions. Workers
claim jobs with expiring leases. On startup, expired leases return to the queue.

## Crash and outage behavior

### Inbound update

1. Long-poll Telegram without advancing beyond uncommitted work.
2. Insert the raw update using its unique Telegram `update_id`.
3. Create the normalized inbox job in the same transaction.
4. Commit locally.
5. Advance the requested Telegram offset.

Crash cases:

- Before commit: Telegram presents the update again.
- After commit but before offset advance: Telegram presents a duplicate; the
  unique `update_id` makes ingestion a no-op.
- During job execution: the lease expires and another worker retries it.

Telegram retains unconsumed bot updates for no longer than 24 hours. Therefore
direct Mac polling handles ordinary internet outages and reboots, but it cannot
guarantee recovery if the Mac is unavailable for more than 24 hours. A later
always-on relay stage closes that gap.

### Outbound delivery

1. The agent/controller writes an outbox row before contacting Telegram.
2. A sender retries transient failures with capped exponential backoff and
   jitter.
3. Permanent failures move to a dead-letter state and surface in `Control`.
4. Successful Telegram message IDs are stored and used for replies and edits.

Telegram does not provide a general idempotency key for `sendMessage` or
`sendVoice`. If the network dies after Telegram accepts a send but before the
Mac receives the response, a conservative retry can produce a duplicate. The
sender will make this rare, label its state as uncertain, and prefer reconciling
known/editable status messages. We should not claim exactly-once network
delivery.

### Agent execution

- Prompts are durable mailbox rows.
- The provider conversation ID is stored after creation and after every turn.
- Project files and Git worktrees persist independently of tmux.
- A tmux session is recreated for observability when useful.
- On restart, running jobs with expired leases become interrupted/retryable.
- The reconciler checks whether a provider session can resume before starting a
  replacement.
- Only one worker owns an agent mailbox at a time.
- Long work emits heartbeats and periodic status updates.

### macOS lifecycle

- Keep the per-user LaunchAgent with `RunAtLoad` and `KeepAlive`.
- Add exponential network retry instead of a fixed tight loop.
- Start the collector, worker, sender, and reconciler as supervised processes
  or one small supervisor with separately observable loops.
- Use atomic database transactions; never store critical state only in memory.
- Add a startup self-check for Keychain, database integrity, free disk space,
  Handy, ffmpeg, Codex, Claude, and tmux.
- After login, reboot, or wake, run reconciliation before accepting new agent
  work.
- Do not prevent sleep. Show an explicit offline/asleep limitation in `/status`.

For unattended operation immediately after a full reboot, a LaunchAgent still
requires the user's graphical login and Keychain availability. We can later
evaluate a LaunchDaemon plus a deliberately designed secret mechanism, but we
should not weaken Keychain protection merely to start before login.

### Backups

- Keep the SQLite database and compact configuration outside the Git checkout.
- Periodically use SQLite's online backup API to create an atomic local backup.
- Retain a small rotating set.
- Exclude bot tokens, temporary audio, and raw generated speech.
- Optionally encrypt and copy backups off-machine after the local system is
  proven.
- On startup, run `PRAGMA quick_check`; if it fails, stop processing and notify
  through a separate recovery path where possible.

## Main Codex router

The main router is Codex, but it is not a forever-growing terminal chat.

Every router turn receives:

- the new normalized text or transcript;
- Telegram surface and reply context;
- a compact list of active project agents and their states;
- known project aliases;
- recent routing decisions relevant to the request;
- a strict set of controller operations it may request.

The response is constrained to structured output, such as:

```json
{
  "action": "route",
  "agent_id": "agent_...",
  "message": "Investigate the webhook test failure",
  "confidence": 0.96
}
```

If confidence is low or a choice is consequential, it asks you with buttons
instead of guessing. The controller validates the structured action before
executing it.

Context freshness:

- durable aliases and policies live in SQLite/configuration, not chat memory;
- completed turns produce short summaries;
- the router receives only relevant state rather than full transcripts;
- rotate to a new Codex session at a measured context threshold or after a
  version/model change;
- seed the replacement with the protocol and current compact state;
- record the rotation as an event;
- keep routing evaluation cases so faster models can be tested before becoming
  the default.

Use the fastest Codex model that passes the routing evaluation suite. Model
choice is configuration, not a hard-coded architectural assumption. Complex
planning belongs in project agents; the main router should usually classify,
clarify, create, resume, or relay.

## Provider adapters

Both adapters expose the same controller contract:

- `create(agent, initial_message) -> provider_session_id`
- `resume(agent, message) -> turn result`
- `status(agent)`
- `cancel(agent)`
- normalized event/output stream

Codex should use its noninteractive JSON/event output and persisted session
resume mechanism. Claude should use print/stream JSON and its persisted session
ID. Interactive keystroke injection is retained only as a temporary
compatibility escape hatch, not the primary protocol.

tmux provides a human-inspectable view or a wrapper around a running adapter.
The controller never infers identity or hierarchy by scraping arbitrary tmux
sessions.

## Voice output

The existing remote Slack voice script is a useful prototype:

- synthesize text with `edge-tts`;
- encode it with ffmpeg;
- upload it as a voice message.

For Telegram:

1. synthesize into a private temporary directory;
2. encode as OGG/Opus suitable for `sendVoice`;
3. enqueue the upload in the durable outbox;
4. attach the same inline keyboard and return route as a text response;
5. delete generated audio after confirmed delivery or terminal failure.

Because `edge-tts` uses an online service, label it as network-dependent.
Evaluate a local TTS adapter later if fully local speech is desired. Agents
produce text plus a response policy; they do not directly execute TTS or hold
the Telegram token.

Voice response modes:

- `off`
- `summary` (default candidate)
- `full`
- `ask`

The mode can be global, per project/topic, or selected on a response button.

## Stages and acceptance gates

### Stage 0 — Preserve and document the POC

Deliverables:

- this Git repository;
- existing bridge, local transcription handler, and LaunchAgent flow;
- no credentials or runtime state in Git;
- basic offline unit tests;
- active service migrated to this checkout.

Acceptance:

- text Mac → phone works;
- text phone → Mac invokes the handler;
- voice phone → Mac returns a Parakeet transcript;
- listener restarts under `launchd`;
- clean Git status after commit.

### Stage 1 — Durable transport

Deliverables:

- SQLite schema and migrations;
- collector that commits before advancing offsets;
- leased inbox worker;
- durable outbox sender;
- callback-query ingestion;
- exponential backoff, dead-letter state, structured logs;
- `telegram-control doctor`, `status`, and `retry`.

Acceptance:

- kill the process before/after each ingest boundary without losing the job;
- submit duplicate update fixtures without duplicate jobs;
- disconnect/reconnect the network and drain queued sends;
- reboot/login and reconcile automatically;
- corrupt/incompatible schema fails safely without consuming updates.

### Stage 2 — Buttons, topics, and return routing

Deliverables:

- inline keyboard builder;
- opaque callback-action registry;
- reply-to-message and topic route bindings;
- `Control`, project, and optional task surfaces;
- menu/status cards that are edited in place;
- authorization and confirmation checks.

Acceptance:

- an agent response's button routes back to that exact logical agent;
- replying to its Telegram message does the same;
- expired/replayed/foreign-user callbacks are rejected;
- routes survive listener restart and tmux teardown.

### Stage 3 — One managed Codex project agent

Deliverables:

- strict agent registry and naming implementation;
- Codex adapter using persisted session IDs;
- one serialized mailbox;
- create/resume/inspect/stop/recreate operations;
- optional tmux view;
- text and voice input routed from one project topic.

Acceptance:

- create the project agent from Telegram;
- complete multiple turns in the same Codex conversation;
- destroy and recreate tmux without losing routing or context;
- restart the controller and continue the same conversation;
- reject invalid paths, names, and unmanaged tmux collisions.

### Stage 4 — Main Codex router

Deliverables:

- structured router prompt and output schema;
- compact durable router state;
- confidence threshold and clarification buttons;
- project aliases;
- router session rotation;
- routing evaluation fixtures and model benchmark.

Acceptance:

- reliably route a held-out set of natural voice requests;
- ask rather than guess on ambiguous cases;
- create a project agent only with explicit, validated project selection;
- rotate router context without changing evaluation results materially;
- meet a measured latency target using the least expensive passing model.

### Stage 5 — Agent hierarchy and provider adapters

Deliverables:

- parent-enforced child creation;
- full appended naming convention;
- Claude adapter;
- worker lifecycle and heartbeats;
- project-agent controls for child inspection and cancellation;
- project/task Telegram surfaces created on demand.

Acceptance:

- project agent creates a correctly named child;
- grandchild name contains the full ancestry;
- main router relays to the project agent by default;
- parent and root views show the complete hierarchy;
- all logical routes survive disposal of every tmux session.

### Stage 6 — Voice responses

Deliverables:

- Telegram `sendVoice` outbox payload;
- TTS adapter based on the existing Slack script;
- summary/full/off policy;
- buttons attached to voice responses;
- cleanup, duration, and size limits.

Acceptance:

- any managed agent can return a playable voice note;
- its buttons and replies return to that agent;
- a TTS/network failure falls back to text without losing controls;
- temporary speech files are removed.

### Stage 7 — Hardening and long-outage option

Deliverables:

- automated fault-injection test matrix;
- online backups and restore drill;
- health/status notifications;
- disk/resource limits and queue pressure policy;
- audit/log redaction review;
- optional tiny always-on HTTPS relay that persists encrypted/minimal Telegram
  updates when the Mac is unavailable longer than Telegram's retention window.

Acceptance:

- restore controller state onto a clean local checkout;
- recover from forced kills, reboot, sleep/wake, and network loss;
- demonstrate known behavior for an outage beyond 24 hours;
- no secret appears in Git, logs, process output, or database backups;
- destructive actions require explicit confirmation and are auditable.

### Stage 8 — Optional Mini App

Only if buttons and topics become limiting:

- searchable agent tree;
- project/session creation form;
- status dashboard and logs;
- model/voice selectors;
- authenticated actions backed by the same route and operation APIs.

The Mini App must not become a second controller. It is another view over the
same durable state machine.

## Test strategy

### Unit

- token and callback parsing;
- authorization;
- name derivation and collision rules;
- state transitions;
- retry/backoff calculation;
- route expiry and one-time consumption;
- provider event normalization.

### Integration with recorded fixtures

- Telegram message, voice, reply, topic, and callback updates;
- duplicate and out-of-order updates;
- fake Telegram API timeouts and ambiguous send results;
- fake Codex/Claude streams and interrupted turns;
- SQLite restart and lease recovery.

### Live smoke tests

Run only through an explicit command:

- send/receive text;
- transcribe a short voice note;
- render and press a harmless button;
- create/resume a disposable test agent;
- return a short voice summary.

### Fault injection

Terminate the collector:

- before database insert;
- after insert but before commit;
- after commit but before offset advance;
- while a worker holds a lease;
- during Telegram send;
- during a provider turn;
- during tmux creation.

Then restart and verify the expected durable state and lack of unintended
duplicate operations.

## Decisions intentionally deferred

- Private bot topics versus a private forum supergroup: implement a common
  `(chat_id, topic_id)` abstraction, then choose after a live UI test.
- Exact fast Codex router model: benchmark against routing fixtures.
- Local versus online TTS: begin by adapting the known working voice script,
  retain an adapter boundary.
- LaunchAgent versus pre-login LaunchDaemon: keep Keychain-safe LaunchAgent
  until pre-login availability is demonstrably necessary.
- Always-on relay hosting: unnecessary for ordinary outages; needed only for a
  hard guarantee beyond Telegram's 24-hour pending-update window.

## Immediate next implementation slice

Stage 1 should be next. Keep its scope narrow:

1. Introduce SQLite migrations and repository classes.
2. Store message and callback updates durably.
3. Advance offsets only after commit.
4. Move the existing text/voice handler behind a leased worker.
5. Put all replies through an outbox.
6. Add deterministic crash-boundary tests.
7. Replace the active LaunchAgent only after the live POC still passes.

Do not add Codex, Claude, tmux, dynamic topics, or TTS during this slice. Durable
transport is the foundation that makes every later capability safe.

## References

- [Telegram Bot API — getting updates](https://core.telegram.org/bots/api#getting-updates)
- [Telegram Bot API — callback queries](https://core.telegram.org/bots/api#callbackquery)
- [Telegram Bot API — inline keyboards](https://core.telegram.org/bots/api#inlinekeyboardmarkup)
- [Telegram Bot API — sendVoice](https://core.telegram.org/bots/api#sendvoice)

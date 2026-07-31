# Telegram Control staged build plan

> **Historical design record:** this file preserves the original staged plan
> and the checkpoints appended while it was built. Statements such as
> “Current proof of concept,” deferred provider support, and the required main
> router describe their point in that history, not the current product.
> Telegram Control now defaults to direct group topics, supports Claude and
> Codex, and keeps the conversational Control router optional. Use the
> [README](../README.md), [documentation index](README.md), and
> [implementation notes](IMPLEMENTATION_NOTES.md) for current behavior.

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

Use one Slam Paws bot identity across every surface:

- the paired private bot chat is the global Control concierge;
- each substantial workspace may use its own private forum group, with the same
  bot added as an administrator;
- topics inside that forum are durable subjects or workstreams;
- smaller or temporary workspaces may share a forum instead of requiring a new
  group.

This gives large workspaces separate, spacious Telegram surfaces without
creating another BotFather token or another controller process. A forum group
maps to an authorized workspace; a topic maps to a lightweight subject
orchestrator. The same `(chat_id, optional message_thread_id)` persistence
model already used by private-chat topics supports all of these surfaces.
Multiple bot identities remain an optional future extension and are not part
of the current release.

Transient buttons remain appropriate for approvals and one-off routes. Worker
sessions normally remain underneath a topic rather than receiving their own
Telegram surface.

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
- The deployed supervisor also runs a daily topic-maintenance reconciler.
  Because the Bot API does not emit ordinary topic-deletion updates or expose
  a read-only topic lookup, it attempts to edit each non-General topic's
  non-editable root service message. `message can't be edited` proves an open
  or closed topic is alive, while `message to edit not found` proves its root
  was deleted. Ambiguous failures fail closed, active work defers cleanup, and
  ordinary Telegram deletion remains the primary user workflow.
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

The first shipped slice uses the least intrusive `ask` behavior: every
completed managed-agent answer remains text-first and offers a one-time
**Listen via Microsoft TTS** button. Speech is generated only after the owner
taps the explicitly labeled control. The
durable `sendVoice` upload has the same agent reply route plus a **Replay**
button; failures retain the text and create a fresh retry control. Generated
files live in an owner-only spool, are atomically published, bounded to 20 MB,
and deleted after confirmed delivery or terminal failure. Stale cleanup
protects every file referenced by a queued or leased upload. Automatic
`summary`, `full`, and `off` preferences remain presentation policy layered
over this path, not a new worker or schema.

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
- Codex native live-control adapter;
- worker lifecycle and heartbeats;
- project-agent controls for child inspection and cancellation;
- project/subject Telegram surfaces bound conversationally.

Acceptance:

- project agent creates a correctly named child;
- grandchild name contains the full ancestry;
- main router relays to the project agent by default;
- parent and root views show the complete hierarchy;
- all logical routes survive disposal of every optional tmux view.

A Claude adapter remains a possible later provider implementation. It is not a
release gate while Codex is the selected provider.

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

- Additional bot identities: use one Slam Paws identity across private forum
  groups first. Add bot-token namespacing only if a real Telegram usability or
  isolation limitation appears.
- Rich provider responses: implemented with a tested Markdown-to-entity
  renderer and formatting-aware chunking. Arbitrary agent output is never
  passed to an HTML or Markdown parse mode, and rejected entities retry as
  plain text. Telegram Rich Messages remain a future transport option rather
  than a prerequisite for safe native formatting.
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

### Implementation checkpoint

The first Stage 1 checkpoint is implemented without replacing the active Stage
0 LaunchAgent:

- SQLite schema version 1 and repository operations;
- atomic message/callback ingestion with the polling offset in the same
  transaction;
- leased inbox and outbox queues with recovery, exponential retry, and
  dead-letter states;
- idempotent durable replies from the existing text/voice handler;
- collector, worker, sender, supervisor, status, doctor, and retry commands;
- offline fault-boundary tests.

The live foreground smoke test against the paired bot passed on July 23, 2026:
one text update and one voice update were committed, processed, and delivered
on their first attempts; local Parakeet transcription succeeded; all queues
drained; and SQLite `quick_check` returned `ok`.

The controlled LaunchAgent migration and forced-restart test also passed on
July 23, 2026. The supervisor recreated the collector, inbox worker, and outbox
sender; the database and polling offset survived; and a post-restart Telegram
message was committed and delivered on its first attempt.

The full reboot/login reconciliation test passed on July 23, 2026. launchd
started the supervisor and all three workers after login, retained the database
and polling offset, recovered from one brief post-boot Telegram network error,
and committed and delivered a new message on its first queue attempts.

This completes the Stage 1 activation checkpoint. Stage 2 buttons, topics, and
durable return routing are next.

### Stage 2 callback checkpoint

The first Stage 2 slice passed offline and live tests on July 23, 2026:

- schema version 2 stores opaque callback actions;
- action creation is idempotent across inbox retries;
- callback data contains only a short random token;
- consumption validates chat, topic, user, expiry, and lifecycle state;
- retrying the same Telegram update may finish safely after a crash;
- a new update replaying a one-time action is rejected;
- the live **Inspect transport** button routed once and the second tap produced
  only the replay notice.

The outbound-message route and reply-to-message slice passed offline and live
tests on July 23, 2026:

- schema version 3 records a route only after Telegram returns the sent
  message ID;
- outbox completion and route creation occur in one local transaction;
- route lookup validates chat, topic, message ID, lifecycle state, and expiry;
- a route survived a forced LaunchAgent restart;
- replying to the exact bot response returned through its stored controller
  route;
- the routed response received its own durable return route.

The surface-binding and editable status-card slice passed offline and live
tests on July 23, 2026:

- schema version 4 binds `(chat_id, message_thread_id)` to a named target;
- an existing surface cannot be silently rebound to another target;
- the same model supports the current private Control chat and future topics;
- `/status` creates the Control binding and a reusable opaque Refresh action;
- two live refreshes edited the same Telegram message ID in place;
- no duplicate card or return route was created.

The persistent singleton-card slice passed 37 offline tests and its live test
on July 23, 2026:

- schema version 5 registers one status card per surface;
- the Telegram message ID survives process and database reopen;
- repeated `/status` commands edit the registered card;
- a permanent Telegram edit failure marks the card stale so the next command
  can register a replacement;
- `topic-capability` reads the private-topic feature flags returned by `getMe`.
- two live `/status` commands created then edited Telegram message ID 40, with
  one active singleton record and no duplicate status-card message.

The managed private-topic slice passed 42 offline tests and its live test on
July 23, 2026:

- BotFather Threaded Mode is enabled and user-created topics are disabled;
- `provision-topic` created **Stage 2 Test**, bound topic ID 62 to
  `controller/control`, and returned that binding on a repeated command without
  creating a duplicate;
- transport normalization ignores Telegram's automatic pseudo-reply to the
  topic-creation service message but preserves genuine user replies;
- a direct topic message resolved the stored surface and replied in topic 62;
- replying to that bot response resolved its durable topic-scoped return route;
- its opaque callback validated topic context and executed once inside the same
  topic.
- `/status` reused the project binding and registered a second singleton card
  for topic 62 without affecting the main Control card.

This completes the Stage 2 buttons, topics, return-routing, and persistent-card
checkpoint. Stage 3 can now introduce the first managed Codex project agent
behind a project topic without changing the Telegram transport model.

The first Stage 3 registry slice passed 45 offline tests and its live test on
July 23, 2026:

- schema version 6 stores immutable random agent IDs, parent relationships,
  roles, provider, project path, provider session ID, surface binding, and
  lifecycle state;
- hierarchical names are controller-derived using the `tc--root--<slug>`
  protocol and strict lowercase slug validation;
- registration requires an existing managed project topic and local Git
  repository, atomically rebinds that surface to the agent, and is idempotent;
- **Stage 2 Test** is registered as `tc--root--telegram-control`;
- live `/agent` inspection reported the project agent as registered with no
  provider session started.

The provider boundary will use structured event/session adapters for ordinary
control. Codex begins with `codex exec --json` and persisted session IDs; Claude
and other harnesses can implement the same contract. tmux remains a separate
human console and explicit takeover/recovery surface, not the durable protocol.

The first provider-adapter and mailbox slice passed 50 offline tests and its
live test on July 23, 2026:

- schema version 7 adds one serialized durable mailbox per managed agent;
- the provider-neutral adapter contract separates session creation/resume,
  structured events, usage, and capabilities from Telegram and persistence;
- the Codex adapter uses JSONL events, checkpoints `thread.started` before
  completion, captures the final public message and token usage, and originally
  defaulted to `workspace-write`; managed project/topic agents now deliberately
  default to `danger-full-access` with approval policy `never`, while the
  global router stays read-only;
- a fourth supervised worker owns agent mailbox leases and routes durable final
  responses back to the bound topic;
- accepted turns produce an immediate receipt while final output is still a
  separate message; a later turn-card slice can edit the receipt in place;
- two live read-only turns succeeded on their first attempts, with a controller
  restart between them and the same persisted Codex session ID on both.

The explicit tmux console slice passed 55 offline tests and its live test on
July 23, 2026:

- schema version 8 stores console reservations independently from agent
  lifecycle and provider-session state;
- only a persisted, idle agent session can be opened, and unmanaged tmux name
  collisions fail closed;
- `starting` and `running` reservations exclude that agent from mailbox claims,
  preventing concurrent structured and interactive control;
- Codex resumes inside tmux with the agent's configured sandbox; the current
  project-agent default is `danger-full-access`;
- `/agent` reports the reconciled console state, and a vanished tmux session is
  released durably;
- the live Codex TUI displayed both earlier structured turns from the persisted
  conversation;
- a Telegram turn received its immediate acknowledgement but remained queued
  while the console ran, then completed on its first attempt immediately after
  `console-close`.

The self-editing turn-message slice passed 57 offline tests and its live test
on July 23, 2026:

- the user-facing receipt is the compact `⏳ Working…`; routing and session
  details remain available through `/agent`;
- receipt creation is atomic with mailbox enqueue and carries durable,
  provider-neutral turn metadata;
- a normal single-message result edits the receipt in place instead of adding a
  second bot response;
- both receipt-first and provider-first races resolve to the same idempotent
  final edit after Telegram supplies the message ID;
- a permanent edit rejection falls back to a normal routed response so the
  final answer is not lost;
- long responses preserve the existing multi-message chunking behavior;
- the live receipt was replaced by the exact Codex response with no duplicate
  final message;
- the first live receipt appeared slowly because that Telegram update reached
  the controller 8.9 seconds after its message timestamp; after ingestion, the
  controller queued it in 0.46 seconds.

`/agent` also reports the normalized input, cached-input, and output token
counts from the latest successful provider turn. The provider adapters now
capture the effective context window directly from Codex app-server or Claude
Agent SDK events, so `/agent`, the initial queued receipt, and generic working
states can show a trustworthy occupied-context percentage without hard-coded
model limits. The queued and working cards label it as the snapshot before the
new turn and also show the effective provider, model, and effort; a new session
or provider switch suppresses stale context metadata.

The managed voice-input slice passed 58 offline tests and its live test on
July 23, 2026:

- a voice update on an agent surface durably creates its single turn card
  before local audio preprocessing;
- Handy/Parakeet transcription becomes the mailbox input for the existing
  persisted provider conversation;
- receipt delivery and transcription/mailbox creation may complete in either
  order without losing the final in-place edit;
- the one Telegram message progresses through `Transcribing`, `Sending` with
  the visible transcript, `Codex is working`, and the final response;
- status edits are durably ordered before the final edit so delayed delivery
  cannot regress a completed turn to an earlier stage;
- non-agent surfaces preserve the existing transcript-only voice behavior;
- a live voice note visibly progressed through transcription, transcript
  sending, Codex work, and the exact final response in one Telegram message.

The first managed lifecycle-control slice passed 59 offline tests and its live
test on July 23, 2026:

- `/agent` exposes opaque, authorized, one-time Pause/Resume and New Session
  actions scoped to the agent's topic;
- paused agents keep accepting durable Telegram inputs but are excluded from
  mailbox claims until resumed;
- pausing fails while a provider turn or interactive console owns the agent;
- starting a fresh provider conversation requires a second confirmation, an
  idle mailbox, and a stopped console;
- resetting changes only the controller's provider-session pointer; it does not
  delete the previous Codex conversation.
- a live paused turn remained queued with zero attempts, completed after
  Resume, and New Session stopped at its separate confirmation without changing
  the persisted session.

The managed-project catalog slice passed 62 offline tests and its live test on
July 23, 2026:

- schema version 9 stores immutable enrolled project IDs, validated slugs,
  display names, providers, canonical Git roots, and lifecycle state;
- terminal enrollment is strict and idempotent, and rejects non-Git paths;
- Telegram `/projects` reveals only catalog names and slugs, never filesystem
  paths;
- `/agent create <slug>` works only inside an existing project topic and only
  for an explicitly enrolled project;
- topic attachment and agent creation are atomic and idempotent, while
  mismatched topics, slugs, and paths fail closed.
- live `/projects` exposed no local path, and live `/agent create
  telegram-control` resolved the existing topic attachment without creating a
  duplicate.

This completes the Stage 3 acceptance checkpoint: managed creation is limited
to enrolled Git projects; repeated turns, restart resume, voice input, explicit
tmux disposal/recreation, pause/resume, inspection, and guarded session
replacement all operate through durable logical agent state.

The Stage 4 router-contract and durable-routing slices pass 88 offline tests:

- router prompts contain only path-safe active catalog metadata;
- normalized output is limited to exact `route`, `clarify`, and `reject`
  schemas with bounded confidence and message lengths;
- route targets and clarification choices must reference enrolled project
  slugs;
- unknown targets, extra fields, markdown wrappers, malformed confidence, and
  duplicate choices fail closed;
- the main agent also receives a compact, typed controller-tool catalog for
  listing or inspecting projects, sending to an existing agent, proposing
  project-agent creation, renaming a managed topic, asking a question, or
  responding directly;
- tool calls use exact JSON envelopes, tool-specific bounded arguments, and
  catalog validation; unknown tools and extra fields fail closed;
- consequential project-agent creation is always marked for
  controller-enforced confirmation rather than left to model discretion;
- consequential topic renaming accepts only a managed `message_thread_id` and
  an explicit 1–128 character name, and is always marked for
  controller-enforced confirmation;
- the command handlers remain recovery and power-user wrappers, not the
  intended conversational interface;
- schema version 10 adds a serialized, leased main-router mailbox whose
  provider session survives controller restarts;
- root Control messages immediately receive one self-editing routing receipt;
  the supervised router worker builds the path-safe prompt, validates one tool
  call, and replaces the receipt with a human-readable preview;
- receipt/result races and failed edits have durable completion and fallback
  paths;
- the first live preview correctly selected `send_to_agent` for a natural
  request and executed nothing;
- validated `send_to_agent` calls are now atomically appended to the selected
  project agent mailbox, and the project response replaces the root routing
  receipt rather than leaking into the project topic;
- `list_projects`, `respond`, and inspection of an enrolled project execute
  directly; inspection reports agent/session/console and Git state without
  exposing its stored path;
- schema version 11 records the authorized router user for durable
  clarification actions;
- `ask_user` choices render as opaque, one-time Telegram buttons; selecting a
  choice expires its siblings and queues a new router turn with the original
  request, question, and answer;
- typed replies to controller-routed messages re-enter the router naturally;
- project creation accepts an enrolled slug or a Git root explicitly present
  in the user's request, revalidates it read-only, derives safe catalog/topic
  metadata, and exposes only opaque authorized Confirm/Cancel buttons;
- confirmation enrolls the project, creates its Telegram topic, and attaches
  the managed agent; cancellation creates nothing;
- confirmed topic renames revalidate the durable `(chat_id, topic_id)` binding,
  call Telegram `editForumTopic`, update the stored display name, and append an
  audit event; cancellation and proposals do not mutate Telegram;
- the main router rotates before a fresh turn once its current Codex session
  reaches 180,000 cumulative input tokens or 12 completed turns, while
  recovery retries remain attached to their original session;
- rotation retains the old Codex conversation, records a durable event, and
  starts the next session directly from reconstructed controller state without
  a separate summarization call;
- controller status reports current router turns, tokens, limits, and rotation
  count;
- root Control voice notes use local Parakeet transcription and one
  self-editing Telegram receipt before entering the same durable main-router
  contract as text;
- unenrolled Git roots can be inspected read-only only when the exact path
  appears in the current user request; the response omits the local path, and
  invented, missing, nested, or non-Git paths fail closed;
- schema version 12 stores globally unique, normalized project aliases;
  conversational alias changes require the alias to appear explicitly in the
  current user message, and dispatch always resolves aliases to canonical
  project slugs;
- a versioned ten-case router fixture suite covers every controller tool and
  can run either fully offline or through isolated live Codex sessions without
  mutating the durable router conversation;
- the default Codex model passed the initial live benchmark 6/6 and the
  alias-expanded benchmark 8/8 on July 23, 2026, with the expanded cold-session
  decisions taking 5.2–10.0 seconds each.

The first Stage 5 provider-adapter slice passes 94 offline tests:

- Claude Code implements the same provider-neutral structured turn contract as
  Codex, using stream-JSON output and durable session UUIDs;
- new and resumed Claude turns validate session continuity, normalize the final
  public result and usage, heartbeat the durable mailbox while streaming, and
  retain the existing crash-recovery prompt;
- Claude permission modes and model overrides are validated controller-side;
  unattended agents default to `bypassPermissions`, while restrictive modes
  remain configurable per agent;
- explicit tmux takeover can resume either Codex or Claude using the same
  logical agent reservation and persisted provider session;
- conversational project creation can explicitly select `codex` or `claude`,
  and still requires the existing validated confirmation;
- model and effort are durable per-agent configuration, can be supplied during
  conversational creation or changed later through `configure_agent`, flow
  through structured and tmux sessions, and appear in `/agent`;
- `/agent` also exposes the active adapter's model and effort catalogs as
  owner-scoped buttons; applying a selection waits for an active turn or
  console to become idle and preserves the current provider-session pointer;
- exact model and effort values must be explicit in the current request;
  subjective choices cause clarification instead of controller invention;
- a live adapter-level Claude probe completed two turns using the same session
  UUID, and the Codex router benchmark passed the expanded 9/9 gate after
  provider/model/effort selection was added.

The durable reply-continuity and reply-context slice passes 145 offline tests
and the 10/10 offline router evaluation:

- schema version 13 records an optional reply surface per agent mailbox turn;
- when a routed project-agent response replaces the root routing receipt, the
  receipt's durable reply route is retargeted to that exact agent inside the
  same transaction that records Telegram's final-edit acknowledgment—never
  before the edit is acknowledged—scoped to the exact chat, topic, and message,
  idempotently, with a durable `route_retargeted` audit event;
- both receipt-first and provider-first races converge on the same retargeted
  route, and a permanently rejected final edit keeps route ownership with the
  main router while the existing fallback message delivers the response;
- replying to the retargeted final message in the root Control chat continues
  the same managed agent and persisted provider session: the reply is enqueued
  only after in-transaction revalidation of the exact stored route, receives
  its own self-editing receipt on the reply surface, and its final message
  routes back to the same agent so follow-ups chain;
- foreign-chat, wrong-topic, wrong-message, expired, and wrong-agent replies
  fail closed without creating mailbox work;
- replies that stay with the main router now embed a bounded (≤1,000
  character), explicitly delimited quote of the replied-to bot message plus a
  provenance label derived only from durable outbox operations; quoted text is
  declared data-not-instructions, spoofed delimiters are stripped, no stored
  paths or secrets are exposed, and explicit-mention validations (aliases,
  models, paths, topic names) evaluate only the user-authored reply text,
  including when a clarification button resumes the original request;
- voice replies resolve the same durable reply routes as text: a voice reply
  to a retargeted agent answer revalidates the exact stored route, continues
  the same agent and persisted provider session with the reply-surface receipt
  and status edits, and a controller-owned voice reply enters the router with
  the same bounded quoted context while status edits display only the user's
  transcript;
- a deterministic reply-dispatch guard prevents quoted bot context from
  authorizing `send_to_agent` by itself: on reply-context turns the dispatch
  runs only when the user-authored reply names the destination by slug,
  display name, or alias; otherwise the controller converts the selection into
  an authorized one-time confirmation question and queues no agent work;
- retried edits answered with Telegram's “message is not modified” complete
  normally so lost acknowledgments still converge on the retargeted route;
- a routing-preview edit can never overwrite the agent's final answer:
  Telegram delivery runs inside an exclusive, non-reentrant kernel advisory
  lock (an `O_NOFOLLOW`, owner-only flock file derived from the canonically
  resolved controller database path) held from atomic lease revalidation and
  renewal — the 600-second lease outlives the API call's hard 180-second
  whole-operation deadline, enforced by running every Telegram request in a
  killable helper subprocess: killing the child bounds every request phase
  (DNS resolution, connect, TLS, header waits, drip-fed success and error
  bodies, and chunked framing alike), including macOS `getaddrinfo`, which
  no in-process signal can reliably interrupt; the helper inherits the
  sender's locked delivery descriptor (`pass_fds`), so the kernel keeps the
  flock held until the helper itself exits — even across a SIGKILLed sender
  or a wake-from-sleep race where no helper thread has been scheduled —
  making inherited lock ownership the delivery-ordering guarantee, proven
  by a regression that SIGSTOPs the helper, SIGKILLs its parent, and shows
  a competing acquirer stays blocked until the helper ends; the helper is
  additionally self-terminating as cleanup — it hard-exits at its own
  wall-clock deadline (so a Mac that slept through the deadline exits on
  wake) or when its payload-identified parent dies; the bot token
  reaches the helper on stdin, never in process arguments, reflected
  descriptions are token-redacted, and standard urllib proxy handling is
  retained — through the API call and its durable
  completion/failure record, so all controller sender processes on this Mac
  deliver strictly one at a time, a paused sender blocks newer edits until
  it resumes, and a crashed sender's lock is released by the kernel; layered
  on that, enqueuing the agent-outcome edit atomically supersedes a
  still-queued preview, edits of the same routing receipt share a typed
  durable `serialize_key` (backfilled for pre-v13 queued rows during
  migration) so the outbox claim never reorders them while leaving all other
  operations unaffected, and a requeued stale preview is completed without
  delivery inside the same critical section — the residual windows are a
  sender process dying, or the deadline expiring, at the exact instant
  Telegram has accepted but not yet applied a request, neither of which any
  client-side mechanism can fence without server-side compare-and-swap;
  both degrade to the documented at-least-once semantics (an edit converges
  through “message is not modified”; a send may rarely duplicate) and never
  to reordering;
- when a receipt was already delivered, multi-chunk responses edit the first
  chunk into the receipt and send the rest as follow-ups; if the response
  finished before the receipt was delivered, the receipt is later resolved
  using the canonical chunking (real content for one normalized chunk, a
  completion marker for several) instead of staying on `⏳ Working…`; a
  permanently rejected first-chunk edit falls back to resending only that
  first chunk;
- an oversized voice-reply transcript keeps its reply-context wrapper (the
  transcript tail is trimmed as a last resort) so the reply dispatch guard
  can never be bypassed by input length.

This slice was verified offline; no live reply-continuity smoke test has been
run yet.

## Chunk: conversational multi-step Control agent

The next tracked chunk (before live steering) replaces the one-shot router
with a bounded, multi-step, identity-transparent Control agent, per the
delegated specification. Deliverables:

1. **Read-only discovery tools.** `find_directory` and `inspect_directory`
   let the Control agent resolve natural path descriptions inside
   user-authorized discovery roots only, with bounded depth/result counts,
   hidden-directory skipping, and strict realpath containment so symlinks
   cannot escape the roots. Results report Git-root status, containing Git
   root, candidate subdirectories, and enrolled-project association.
2. **Bounded durable multi-step routing.** One router turn may make several
   discovery calls before exactly one terminal outcome (respond, ask_user,
   confirmed-mutation proposal, or dispatch). Steps are persisted on the
   router mailbox as they complete; a crash-recovery retry resumes from the
   persisted steps instead of restarting blind. Step-count, elapsed-time,
   and discovered-path bounds terminate the loop with a precise message.
3. **Controller-issued provenance.** Every discovered directory receives an
   opaque controller-issued reference ID persisted with the turn. Mutation
   proposals may identify paths only by those IDs or by verbatim presence in
   the user's own text — a model-asserted path with no issued ID fails
   closed. Confirmation payloads carry `{value, source, derived_from}`
   provenance for audit.
4. **repository_root / working_directory split.** Schema v14 rebuilds
   `managed_projects` without repository-path uniqueness (sibling working
   directories in one repository are allowed; slugs stay unique), adds
   `working_directory` to projects and agents backfilled to the repository
   root, and expires legacy in-flight project-confirmation callbacks whose
   payloads predate the split. Centralized validation requires both paths to
   exist, the root to be a real Git root, and the working directory to be
   contained in the root after symlink resolution — enforced at proposal,
   confirmation (TOCTOU re-check), and launch time. Adapters and tmux start
   in `working_directory`; Git validation uses `repository_root`.
5. **Execution-boundary safety.** Project enrollment/creation, topic
   renames, and now agent configuration changes are all confirmation-gated
   with authorized one-time buttons; the agent can reason freely but can
   never claim a mutation succeeded unless the controller executed it.
   Schema v15 adds a durable mutation saga around Telegram project-topic
   creation and topic renaming: a compare-and-set external claim admits only
   one concurrent confirmation, successful API results resume local
   application after a crash without repeating Telegram, and ambiguous lost
   results enter an explicit reconciliation state instead of risking a
   duplicate mutation.
6. **Identity and handoffs.** Central speaker rendering labels every
   Control-chat turn: `🎛 Control`, `🎛 Control → {Project}` on dispatch,
   and `{Project}` on relayed or reply-continued agent responses.
7. **Precise outcomes.** The "Router preview / Would inspect…" fallbacks are
   removed from normal conversation; every response states exactly what was
   done, found, or still needs validation. Topics still bound to
   `controller/control` now route to the main router instead of the
   transport-test acknowledgment.

Acceptance criteria (offline fixtures; no live mutation without user
confirmation): the Lovely request ("the Peter app subdirectory of the lovely
repo in software inside my user directory") resolves the repository and
`peter-app` working directory via discovery and produces a confirmation-backed
proposal without requiring verbatim absolute paths; ambiguous references
produce a clarification listing concrete candidates; forged provenance and
escaped symlinks fail closed; bounded loops terminate precisely and resume
correctly after a crash; v13 databases migrate atomically through schemas 14
and 15 while preserving all IDs, sessions, topics, and aliases; sibling
working-directory projects coexist; confirmations are idempotent even under
concurrent delivery; and every response identifies its speaker.

The conversational Control chunk is implemented: 179 offline tests and the
13/13 offline router evaluation pass, covering the deterministic
Lovely/peter-app fixture, candidate-listing clarification for ambiguity,
forged-path and forged-ref rejection, bounded-loop termination and
crash-retry resume from persisted steps, the v13→v15 migration (identity
preservation, legacy project-confirmation expiry, sibling working
directories), TOCTOU symlink-swap rejection at confirmation, configuration
confirmation flow, speaker labeling, and concurrent/crash-recovery mutation
sagas for both Telegram project creation and topic renaming. Schema v14 rebuilds
`managed_projects` without repository-path uniqueness, adds working
directories to projects and agents, and persists per-turn discovery state;
schema v15 persists the Telegram mutation boundary and reconciliation state.
No live Lovely mutation is performed without user confirmation.

## Chunk: workspace agents and full conversational Control

Schema v16 removes the accidental Git-only enrollment rule. The durable
`project_path` column remains in place for migration compatibility but is now
the agent's confirmed `workspace_root`; `git_repository_root` is nullable
metadata. Existing enrolled coding projects are backfilled as Git workspaces,
while new agents may attach to any existing authorized directory, including a
notes tree such as `~/life`. The working directory must remain inside that
workspace after realpath resolution at proposal, confirmation, and every
launch. The filesystem root is never enrollable, a containing parent
repository never widens the boundary, and consequential creation still
requires an authorized one-time confirmation.

Control should feel like an agent, not a command parser. The current persistent
Codex turn is instructed to answer and reason normally, with the JSON tool
envelope treated as private controller plumbing. The next refinement replaces
that envelope with Codex app-server dynamic tools: ordinary assistant text
becomes the response directly, while validated controller operations are
offered as callable tools that can be used repeatedly in one turn. Mutations
continue to create confirmation proposals rather than gaining direct Telegram,
database, or credential access.

## Chunk: subject orchestrators

The durable unit presented in Telegram is a **subject orchestrator**, not a
worker session and not necessarily a Git project. The paired Slam Paws chat is
the global orchestrator. A private forum group may bind to an authorized
workspace such as `~/life` or a software repository, and each topic in that
group may host a subject orchestrator scoped within that workspace.

A subject orchestrator:

- converses with the user and reasons about ambiguous or multi-step requests;
- owns the subject's durable purpose, workspace boundary, decisions, active
  jobs, worker results, and a compact rolling memory;
- exposes validated controller capabilities as native Codex tools;
- delegates substantive execution to Codex workers instead of doing the work
  in its own conversational turn;
- receives worker progress and results, summarizes them for the user, and can
  steer, stop, retry, or create additional workers;
- survives provider-session rotation by reconstructing a fresh session from
  durable subject memory. Underlying provider sessions may compact or rotate,
  but that lifecycle is invisible at the Telegram product layer.

Workers are execution records underneath an orchestrator. They may be
ephemeral or durable and may use different working directories within the
orchestrator's authorized workspace. They do not receive Telegram topics by
default; a separate topic is created only when the user explicitly wants a
long-lived conversational surface for that worker or sub-subject.

The first release keeps this deliberately small: one bot token, one controller
process, one SQLite database, one global Control orchestrator, one workspace
binding per private forum group, and one lightweight subject per provisioned
topic. Subject turns may use a current Codex worker directly; the
orchestrator/worker distinction is logical and does not require a separate
daemon or actor runtime. Hierarchical sub-subjects, automatic worker fan-out,
multiple bot identities, and richer long-term memory are extensions rather
than prerequisites for basic messaging and voice use.

Remaining release work:

1. ~~Detect and conversationally bind private forum groups to arbitrary
   workspaces; Git metadata remains optional.~~ Completed in schema v18.
2. Give each forum topic lightweight durable subject state and conversational
   worker/session controls. Schema v19 implements subject provisioning and
   reuses the existing worker controls. Provider silence is bounded by the
   adapter deadline, worker crashes recover through leases, transient failures
   replace the live card with a retry state, and terminal failures replace it
   with an actionable error; no separate watchdog actor is needed.
3. Add adoption of discoverable existing Codex and Claude sessions, voice replies, and
   status presentation polish. Session adoption is explicit and
   confirmation-gated: `/agent` can list a bounded set of recent persisted
   provider sessions whose recorded working directory exactly matches the topic
   agent. The controller revalidates the selection and requires an idle
   mailbox, a stopped managed console, and exclusive ownership before changing
   only that agent's provider-session pointer. Controller-created sessions and
   sessions already attached elsewhere are omitted. This resumes persisted
   context; it deliberately does not claim that a concurrently active Codex or
   Claude window can be seized through a second provider process. Discovery
   and confirmation revalidation use capped directory traversal, file counts,
   metadata bytes, index bytes/lines, and wall time so accumulated provider
   history cannot monopolize the Telegram inbox worker. `/agent` also supports
   in-place model and effort changes sourced from the active Codex or Claude
   adapter, plus a confirmation-gated switch between providers for an idle
   topic. In-place configuration preserves the active conversation; switching
   providers leaves the old conversation persisted locally while the new
   provider starts fresh unless the user explicitly adopts one of its existing
   sessions. Dormant Codex-session adoption has passed a live
   context-continuity test; Claude parity is implemented and awaiting the same
   live acceptance check. The first voice-output
   slice is implemented as on-demand **Listen** / **Replay** controls with
   durable `sendVoice`, text fallback, bounded private speech files, and
   cleanup; live playback and reply-route verification remain before choosing
   whether automatic per-topic `summary` / `full` / `off` preferences add
   enough value.
4. Run the full acceptance matrix and an independent Codex review before each
   live schema migration and deployment.

### Live-control checkpoint

Schema v17 live progress, reply-to-steer, and Stop passed the production
Telegram acceptance test on July 24, 2026. Replying to the exact active card
delivered guidance to the stored Codex provider turn and changed its final
answer. A separate long-running turn moved from Working to Stopping to
Cancelled after the one-time Stop action; Codex acknowledged the interrupt,
the mailbox remained terminally cancelled, and no late result or retry
appeared.

### Private-forum transport checkpoint

The next slice keeps one Slam Paws identity and admits only the paired owner in
a private forum supergroup topic. Public groups, non-forum groups, channels,
unrelated private chats, and other users fail before their content is written
to SQLite. A new forum does not reach the global router immediately: its first
text or voice request produces an exact user/chat/topic-bound **Authorize
forum** action. When the first text message is itself an explicit setup request
with a validated local path, the controller may instead offer one
**Authorize and bind** action that atomically creates the Control surface and
forum workspace. Otherwise the confirmed forum continues through the existing
conversational binding flow.

Telegram enables Group Privacy by default, so Slam Paws must be promoted to
administrator in each private forum before ordinary topic text and voice can
reach the controller. This is an explicit live-setup requirement, not
something the local tests can simulate.

### Forum-workspace binding checkpoint

Schema v18 adds one durable workspace boundary per authorized private forum.
The conversational Control agent receives bounded current-surface state and
may propose `bind_forum_workspace` only for the forum containing the request.
The proposal accepts an enrolled workspace, an explicit user path, or an
opaque result from read-only discovery; Git remains optional and both Codex
and Claude are supported. Model and effort overrides must appear explicitly
in the user's request.

The controller revalidates the forum authorization and every realpath at
confirmation time, then persists the binding only after the owner presses the
one-time **Bind forum workspace** button. Exact retries are idempotent, while
cross-chat confirmation, model-invented paths or discovery refs, symlink
movement, false Git metadata, callback replay, and silent rebinding fail
closed. Schema-17 migration and end-to-end request/proposal/confirmation
fixtures cover those boundaries before live deployment. The permanent-edit
fallback also preserves the active confirmation keyboard if Telegram can no
longer edit the original routing receipt. A workspace binding alone still does
not create workers; schema v19 provisions each topic lazily on its first
ordinary message.

### Forum-subject checkpoint

Schema v19 adds one durable `forum_subjects` record per provisioned topic in a
bound private forum. The first ordinary text or voice message atomically
creates the subject, converts the topic surface to a task route, and attaches a
managed Codex worker that inherits the forum's validated workspace, working
directory, optional exact Git root, and provider configuration. Subsequent
messages reuse the same agent and persisted provider session. Topic display
name changes update the subject and route labels without replacing the
subject's durable identity.

This deliberately reuses the existing agent mailbox, progress-card,
reply-to-steer, Stop, pause/resume, and new-session machinery. It does not add
another controller process, queue, daemon, or actor framework. Until a forum
has a confirmed workspace binding, its topics remain Control surfaces so the
global router can complete the conversational binding flow. Commands and exact
replies do not trigger provisioning: `/status` remains read-only, existing
project-agent topics retain their current route/session, and historical
Control reply routes continue to reach Control with bounded text or voice
context after a topic has become a subject.

### Lightweight execution and agent-update checkpoint

The controller keeps one queue per persisted agent but now supervises eight
agent workers, allowing different topic agents to execute concurrently while
preserving single-writer ordering within each conversation. The pool is
configurable from 1 through 16. Managed Codex
agents default to `danger-full-access` with approval policy `never`; managed
Claude agents default to `bypassPermissions` with
`--dangerously-skip-permissions`. The global router remains read-only.

An active managed turn receives only its database path, agent ID, mailbox ID,
and lease-owner ID as runtime context. The provider-neutral
`agent_telegram.py` helper uses that context to verify the live lease and
durably enqueue a concise text or voice update to the owning topic. No bot
token is passed through the helper. Two focused, implicitly triggered skills—
`telegram-voice-message` and `telegram-text-update`—are installed for Codex
and Claude and retained canonically in this repository. Users can ask for a
voice note or progress update naturally without naming either skill. This
deliberately avoids a second notification service or provider-specific
Telegram integration.

## References

- [Telegram Bot API — getting updates](https://core.telegram.org/bots/api#getting-updates)
- [Telegram Bot API — callback queries](https://core.telegram.org/bots/api#callbackquery)
- [Telegram Bot API — inline keyboards](https://core.telegram.org/bots/api#inlinekeyboardmarkup)
- [Telegram Bot API — editForumTopic](https://core.telegram.org/bots/api#editforumtopic)
- [Telegram Bot API — sendVoice](https://core.telegram.org/bots/api#sendvoice)

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
- Rich provider responses: controller-owned statuses may use escaped Telegram
  HTML now. Before styling arbitrary agent output, build fixture coverage for a
  safe Markdown-to-entity renderer and evaluate Telegram Rich Messages. Never
  pass untrusted model text directly to an HTML or Markdown parse mode.
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
  completion, captures the final public message and token usage, and defaults
  to `workspace-write` rather than unrestricted/yolo execution;
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
- Codex resumes inside tmux with the agent's configured sandbox, without
  enabling unrestricted/yolo mode;
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
counts from the latest successful provider turn. It intentionally does not
claim a context-window percentage until the adapter can identify the effective
model and its limit reliably.

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

The Stage 4 router-contract and durable-preview slices pass 74 offline tests:

- router prompts contain only path-safe active catalog metadata;
- normalized output is limited to exact `route`, `clarify`, and `reject`
  schemas with bounded confidence and message lengths;
- route targets and clarification choices must reference enrolled project
  slugs;
- unknown targets, extra fields, markdown wrappers, malformed confidence, and
  duplicate choices fail closed;
- the main agent also receives a compact, typed controller-tool catalog for
  listing or inspecting projects, sending to an existing agent, proposing
  project-agent creation, asking a question, or responding directly;
- tool calls use exact JSON envelopes, tool-specific bounded arguments, and
  catalog validation; unknown tools and extra fields fail closed;
- consequential project-agent creation is always marked for
  controller-enforced confirmation rather than left to model discretion;
- the command handlers remain recovery and power-user wrappers, not the
  intended conversational interface;
- schema version 10 adds a serialized, leased main-router mailbox whose
  provider session survives controller restarts;
- root Control messages immediately receive one self-editing routing receipt;
  the supervised router worker builds the path-safe prompt, validates one tool
  call, and replaces the receipt with a human-readable preview;
- receipt/result races and failed edits have durable completion and fallback
  paths;
- the preview executes no tool; live evaluation comes before enabling
  read-only tools, clarification buttons, confirmations, or real dispatch.

## References

- [Telegram Bot API — getting updates](https://core.telegram.org/bots/api#getting-updates)
- [Telegram Bot API — callback queries](https://core.telegram.org/bots/api#callbackquery)
- [Telegram Bot API — inline keyboards](https://core.telegram.org/bots/api#inlinekeyboardmarkup)
- [Telegram Bot API — sendVoice](https://core.telegram.org/bots/api#sendvoice)

# Implementation notes

This is the accumulated, stage-by-stage record of how Telegram Control was
built and what was verified live at each step. It is written in the order the
work happened, so it is the place to look for the precise behavior, invariants,
and failure semantics of a specific mechanism — not the place to start.

- New here, or setting the project up? Read [../README.md](../README.md).
- Looking for the original design and its acceptance gates? Read
  [BUILD_PLAN.md](BUILD_PLAN.md).

Two reading notes: shell examples are relative to the repository root, so run
them from a checkout (`cd /path/to/telegram-control`), and **Slam Paws** is
simply the author's bot — read it as "your bot" throughout. Nothing in the code
depends on that name.

Telegram Control is a phone-to-Mac control plane for local Codex and Claude
agents. Its current default experience is:

1. Bind a private Telegram forum group to an exact local workspace.
2. Send text, a photo, a document, or an optional voice note in one of its
   topics.
3. Route the request directly to that topic's persisted Claude or Codex agent.
4. Receive a formatted text answer with contextual controls and optional
   spoken replies.

The central conversational Control router remains available as an opt-in
feature for natural-language discovery and delegation; it is disabled by
default. The sections below are chronological, so an earlier mechanism may be
explicitly superseded by a later section.

## Stage 0 baseline

- Computer → phone text messages.
- Phone → computer text messages that invoke a fixed local Python handler.
- Phone → computer voice notes transcribed locally with Parakeet V3.
- A macOS LaunchAgent that starts the listener at login and restarts it if it
  exits.
- Bot token storage in macOS Keychain.
- Authorization restricted to the Telegram user confirmed during pairing.
  That owner may use the paired bot chat or private groups that contain the
  bot; other users and public groups are ignored.

Stage 0 intentionally does not yet route messages into Codex, create agents,
send synthesized voice responses, provide buttons, or guarantee durable job
processing. Those are staged in the build plan.

## Stage 1 development path

The first durable-transport slice is now implemented alongside Stage 0. It
adds:

- a versioned SQLite store using WAL, foreign keys, full synchronization, and
  an integrity check;
- atomic update ingestion and polling-offset advancement;
- post-commit mirroring of the offset for a safe fallback to Stage 0;
- message and callback-query jobs with expiring leases, retry backoff, and a
  dead-letter state;
- an idempotent outbox for Telegram API calls;
- separate collector, inbox-worker, and outbox-sender loops;
- deterministic tests for duplicate ingestion, transaction rollback, worker
  death, stale leases, and durable handler replies.

Initialize a development database and inspect it:

```sh
./telegram_control.py init
./telegram_control.py status
./telegram_control.py doctor
```

Run the durable transport in the foreground:

```sh
./telegram_control.py run
```

After the live foreground smoke test passes, migrate the existing LaunchAgent
in place:

```sh
./telegram_control.py install
```

The durable installer retains the existing LaunchAgent label so the Stage 0
poller is unloaded before Stage 1 starts. Do not run Stage 0 `listen` alongside
the Stage 1 collector because Telegram permits only one long poller for a bot.
Restore Stage 0 at any time with:

```sh
./telegram_bridge.py install \
  --handler ./on_message.py
```

The active installation was migrated to the durable controller on July 23,
2026. A forced LaunchAgent restart recreated the supervisor and all three
workers, preserved the polling offset and queue state, and successfully
processed a new Telegram message after restart.

A full reboot/login test also passed on July 23, 2026. launchd started the
durable supervisor and all three workers automatically after login. A brief
post-boot network failure was retried automatically, and the first Telegram
message after reboot was committed, handled, and delivered without a queue
retry.

## Stage 2 callback checkpoint

Schema version 2 adds durable opaque callback actions. Text acknowledgments now
include an **Inspect transport** button whose compact `a:<token>` callback data
contains no command, path, prompt, or privileged payload. SQLite holds the
actual action and validates its chat, topic, authorized user, expiry, and
one-time state.

The live button test passed on July 23, 2026. The first tap resolved and
consumed the stored action and returned its routed response. A second tap
created a separate durable callback update but returned only “This button was
already used” without executing the action again.

Schema version 3 adds durable outbound-message return routes. After Telegram
confirms a `sendMessage` call, the outbox sender atomically binds the returned
Telegram message ID to its local target, chat, topic, policy, and expiry. Reply
messages resolve that binding from SQLite; none of those internal target
details appear in Telegram.

The live reply-routing test passed on July 23, 2026. The route was created,
survived a forced LaunchAgent restart, and then routed a Telegram reply back to
the controller. The routed response received its own return route.

Schema version 4 adds durable surface bindings keyed by Telegram chat and
optional topic. `/status` binds the current private chat to the Control target
and sends an editable status card with a reusable opaque Refresh action.

The live status-card test passed on July 23, 2026. Two consecutive Refresh taps
edited the same Telegram message ID in place, kept the same active action and
return route, and created no duplicate card.

Schema version 5 persists one singleton card record per surface and card type.
Repeated `/status` commands edit the registered Telegram message instead of
creating additional cards. If Telegram reports that the message was deleted or
can no longer be edited, the record becomes stale and the next `/status`
creates and registers a replacement.

The live singleton test passed on July 23, 2026. The first `/status` registered
Telegram message ID 40 and the second `/status` edited message 40 in place
without creating another status card.

Check whether BotFather's Threaded Mode is enabled before provisioning private
chat topics:

```sh
./telegram_control.py topic-capability
```

Create and durably bind a managed project topic:

```sh
./telegram_control.py \
  provision-topic "Project Name"
```

Provisioning the same name again returns its existing binding instead of
creating a duplicate topic. Ordinary top-level topic messages ignore
Telegram's automatic reply link to the topic-creation service message, while
explicit replies to bot messages continue through their stored return routes.

The live topic test passed on July 23, 2026. The controller created and bound
**Stage 2 Test** as topic ID 62. A direct message, an explicit reply, and an
opaque callback button all routed successfully within that topic. Repeating
the provisioning command returned the same binding without creating another
topic. `/status` in the project topic created its own singleton card while
preserving the separate main Control card.

## Stage 3 agent registry checkpoint

Schema version 6 adds a provider-neutral managed-agent registry. Logical
identity uses random immutable `agent_id` values; display and tmux names use the
validated hierarchy `tc--root--<slug>`. Register an existing managed project
topic against a local Git repository with:

```sh
./telegram_control.py \
  register-agent "Project Topic" project-slug /absolute/project/path
```

Registration atomically creates the project agent and changes the topic binding
from `controller/control` to `agent/<agent_id>`. Repeating the same registration
returns the existing agent; mismatched registrations and invalid slugs fail
closed. Send `/agent` inside the topic for a path-safe registry, session,
console, and latest provider-usage summary. Send `/teardown` inside an active
managed topic to open its durable cleanup confirmation without invoking an
LLM. Send `/help` anywhere Control is
available to open an editable, button-driven guide to commands, managed
agents, detached workers, voice updates, installed skills, and topic teardown.
Setup and introductory messages include the same `/help` hint.

The live registry test passed on July 23, 2026. **Stage 2 Test** is bound to
`tc--root--telegram-control` using the Codex provider and reports `registered`
with no provider session started.

Schema version 7 adds a serialized durable mailbox per managed agent and a
provider-neutral execution contract. The first adapter uses structured JSONL
from `codex exec --json`, checkpoints `thread.started` before turn completion,
parses the final public agent message and usage metadata, and resumes the stored
session ID. Managed project and topic agents now run with
`danger-full-access` and approval policy `never` by default. This is the Codex
equivalent of the user's normal `--yolo` workflow. The central conversational
router remains explicitly read-only.

The Stage 5 provider expansion adds Claude Code behind that same contract.
Claude runs in non-interactive stream-JSON mode, checkpoints its session UUID
before completion, normalizes the final result and usage, and resumes the same
conversation on later mailbox turns. Managed Claude agents default to
`bypassPermissions` plus `--dangerously-skip-permissions` because unattended
turns cannot answer permission prompts; set `provider_config.permission_mode`
to `acceptEdits`, `auto`, `dontAsk`, or `plan` for a more restrictive agent.
The explicit tmux console can resume either a Codex or Claude session without
changing its logical agent identity. The `/agent` card also exposes the same
provider-neutral lifecycle directly in Telegram. **Change model / effort**
uses choices owned by the active Codex or Claude adapter and applies them to
subsequent turns while preserving the current provider session. Reconfiguration
waits for any active structured turn or tmux console to become idle. The topic
can also switch between Codex and Claude after confirmation, start a fresh
conversation, or choose a recent dormant session from its exact working
directory. Switching providers starts a fresh conversation but does not delete
the previous local session.

Eight supervised agent workers may lease different agents concurrently. The
mailbox still serializes turns for each individual agent, so two topics can
work at once without allowing two processes to mutate the same persisted
conversation concurrently. The foreground `run --agent-workers` option accepts
1 through 16 when a different local resource limit is appropriate.

Each accepted agent turn immediately sends a compact queued receipt. When the
current provider session has reported context metadata, that first receipt and
the generic working states include a `Context before this turn` snapshot with
the percentage used and token/window counts. Those transient states also show
the effective provider, model, and effort so inherited defaults are visible
before streamed output replaces the card. Progress edits that same Telegram
message while the provider runs. Completion sends the final answer as a new
routed message so Telegram can notify the user, then durably queues deletion of
the progress receipt only after Telegram acknowledges the last final-response
chunk. The receipt and completion paths are race-safe if the provider finishes
first, and a failed final send retains the progress card while the response
retries.

The live adapter test passed on July 23, 2026. Two read-only Telegram turns
completed through the serialized mailbox on their first attempts. The
controller was restarted between turns, both used the same persisted Codex
session ID, and the second turn produced an immediate receipt followed by the
final response.

Schema version 8 adds an optional interactive tmux console. Structured adapter
turns remain the normal control path; the console is an explicit takeover of an
already-persisted session:

```sh
./telegram_control.py \
  console-open tc--root--telegram-control
./telegram_control.py \
  console-status tc--root--telegram-control
tmux attach-session -t '=tc--root--telegram-control'
./telegram_control.py \
  console-close tc--root--telegram-control
```

Opening fails unless the agent has a persisted provider session and an idle
mailbox. The reservation prevents the mailbox worker from controlling the same
agent concurrently. New Telegram turns may still queue durably during takeover
and are claimed after the console closes. Existing unmanaged tmux sessions are
never adopted or killed, and the console retains the agent's configured
permission mode.

Project-creation requests may explicitly name `codex` or `claude`. The
controller validates that provider and shows it in the existing confirmation
card before enrolling the repository, creating its topic, or attaching its
agent.

Model and effort are also conversational, durable agent settings. A creation
request can name them directly, and an existing project agent can be updated
with a request such as `Use gpt-5.6-sol with high effort for TC`. Omitted
settings retain the provider default; explicit `null` resets a setting. The
controller validates provider-specific effort levels, requires model names to
be explicit, lets the installed harness reject unavailable models, carries the
settings through structured and tmux turns, and displays them in `/agent`.
Subjective requests such as “use the best model” are routed to clarification
instead of silently choosing a potentially expensive model.

`/agent` also displays the current session's latest occupied-context percentage
and token/window counts. Codex supplies both values through app-server token
usage notifications; Claude supplies its model context window and the latest
main-message usage through Agent SDK events. The controller does not hard-code
model limits, and it suppresses old context metadata after a new session or
provider switch until the replacement session completes a turn.

The live console test passed on July 23, 2026. The real Codex TUI resumed the
same conversation in tmux, `/agent` reported `Console: running`, and a Telegram
turn remained queued until `console-close`. It then completed on its first
mailbox attempt with the expected response.

The original live self-editing turn test passed on July 23, 2026. The completion
policy now deliberately uses a new final message followed by acknowledged
progress-card cleanup so completion can produce a Telegram notification without
leaving status noise behind. One observed slow receipt was traced to an isolated
8.9-second delay before the Telegram update reached the controller; the
controller queued the receipt within 0.46 seconds of ingestion.

While a managed Codex or Claude turn is running, that same receipt now shows
the provider's user-facing commentary and response text as it streams. Codex
`commentary` and `final_answer` agent-message phases and Claude text content
blocks are accumulated incrementally; reasoning, tool inputs, and other
non-user-facing events remain generic status updates. Rapid deltas are bounded
and coalesced in the durable outbox so they do not create a Telegram edit
backlog. The completed answer then cleanly replaces the intermediate text on
screen as a new message, after which the original receipt is deleted.

Voice notes sent inside a managed agent topic use that same turn card. The
`🎙️ Transcribing…` receipt is queued before local download, ffmpeg conversion,
and Parakeet V3 transcription. The same message then shows `📤 Sending:` with
the transcript, changes to `🧠 Codex is working…` or
`🧠 Claude is working…`. The agent response is sent as a new message and the
progress receipt is deleted after that send succeeds. Durable outbox ordering
prevents a delayed progress edit from racing the final answer or its cleanup.
Voice notes outside a managed agent surface retain the original transcript-only
behavior.

The live managed-voice test passed on July 23, 2026: one voice note progressed
through every status stage and returned the requested exact Codex response.

Transient Telegram transport failures during voice download use a dedicated
handler exit status rather than publishing `❌` from an attempt that the inbox
will retry. Nonterminal failures leave the idempotent transcribing receipt in
place. If all five durable inbox attempts fail, the same receipt is edited to a
terminal request to resend the voice note (or a new terminal message follows it
if Telegram never acknowledged the receipt). This prevents a recovered retry
from leaving a contradictory internet-connection warning above successful
agent work. Verified on July 26, 2026 with tests for silent failure followed by
success, receipt editing only after dead-lettering, transport-error
classification in the handler, and preservation of the retryable subprocess
exit across the bridge.

Controller-owned progress text uses Telegram HTML formatting with escaped
dynamic content: stage labels are bold and voice transcripts are block quotes.
Provider output uses a tested Markdown-to-entity renderer with a documented
subset and plain-text fallbacks; arbitrary model text is never passed directly
to a Telegram parse mode.

`/agent` includes one-time, topic-bound lifecycle buttons. **Pause** prevents
new mailbox claims while allowing Telegram inputs to queue durably; **Resume**
drains them again. **New session…** requires a second confirmation, an idle
mailbox, and a stopped console before clearing the controller's session pointer.
The prior Codex conversation is retained by Codex and is not deleted.

The live lifecycle test passed on July 23, 2026. A paused turn remained durably
queued with zero attempts and completed after Resume. Opening New Session
displayed its second confirmation without changing the current session.

Schema version 9 adds a path-safe managed-project catalog. A local Git
repository must first be explicitly enrolled from the terminal:

```sh
./telegram_control.py \
  enroll-project project-slug /absolute/project/path --name "Project Name"
```

Telegram exposes only the catalog slug and display name through `/projects`;
it never accepts or displays an arbitrary filesystem path. Within an existing
provisioned project topic, `/agent create project-slug` atomically attaches the
enrolled project and is idempotent when the correct agent is already present.

The live catalog test passed on July 23, 2026. Control listed the enrolled
`telegram-control` slug without its path, and the topic creation command
recognized the existing matching agent without creating a duplicate.

## Stage 4 router contract

The main-router boundary now has a small typed controller-tool vocabulary:
list or inspect projects, send work to an existing agent, propose creating a
project agent, rename a managed Telegram topic, ask a concise question, or
respond directly. The prompt includes only active project slugs, display names,
providers, compact agent state, and managed topic names/IDs—never catalog
filesystem paths or chat IDs. Every tool call is strict JSON and is validated
again by the controller. Unknown tools, projects, topics, arguments, extra
fields, and oversized values fail closed; project-agent creation and topic
renaming always require controller-enforced confirmation.

Ordinary messages in the root Control chat now enter a dedicated durable
router mailbox. A supervised worker invokes the main Codex agent with the
path-safe catalog, persists its provider session, validates the selected tool,
and replaces a `🧭 Routing…` receipt with the selected action. A validated
`send_to_agent` call is atomically appended to the project agent's durable
mailbox. The root receipt briefly reports the destination, then is replaced by
the project agent's eventual response. Project-agent creation remains
confirmation-gated.

The read-only `list_projects`, enrolled-project `inspect_project`, and
`respond` tools also execute directly. Project inspection reports compact
agent, provider-session, console, branch, and working-tree state without
exposing the enrolled filesystem path. An unenrolled Git root can also be
inspected read-only when its path appears explicitly in the current user
request; invented, missing, nested, and non-Git paths still fail closed.

The `ask_user` tool renders its bounded choices as authorized, one-time
Telegram buttons. Selecting one expires the sibling choices and queues a new
durable router turn containing the original request, question, and answer.
Typed replies to controller-routed messages also re-enter the main router
instead of falling back to a transport-test acknowledgment.

Conversational project creation accepts either an enrolled slug or a local Git
repository path that appears explicitly in the user's request. The controller
re-resolves and validates the repository root, derives a safe catalog entry,
and shows the proposed provider and Telegram topic behind **Create project
agent** and **Cancel** buttons. Only the authorized confirmation enrolls the
project, creates its private-chat topic, and attaches the managed agent.

Conversational topic renaming uses the managed binding's stable
`message_thread_id`; the requested name must appear explicitly in the current
message. The controller presents the current name, new name, and topic ID
behind authorized one-time **Rename topic** and **Cancel** buttons. Confirmation
revalidates the binding, calls Telegram `editForumTopic`, then atomically
updates and audits the durable display name. Cancellation and unconfirmed
proposals do not call Telegram.

Enrolled projects can also have durable conversational aliases. Natural
requests can add or remove an alias when the alias is stated explicitly in the
current message. Aliases are globally unique, survive restarts and router
rotation, appear in the path-safe router catalog, and always resolve back to a
canonical project slug before work is dispatched.

The main router rotates before the next fresh turn after either 12 completed
turns or 180,000 cumulative input tokens reported by Codex. Rotation clears
only the controller's active session pointer; the old Codex conversation is
retained. The next ordinary turn starts a new read-only Codex session from the
tool protocol, current catalog and agent state, and current request—there is no
extra summarization turn. Crash-recovery attempts always resume their attached
session instead of rotating mid-recovery. CLI and Telegram status expose the
current counters and durable rotation count.

Voice notes sent to the root Control chat now enter the same durable router
mailbox as text. One Telegram message progresses through local transcription,
escaped transcript display, main-router work, and the final routed response.
The router session, validation, clarification, confirmation, and project-agent
relay rules are identical for text and voice input.

The router contract has a repeatable ten-case quality gate covering every
controller tool. The offline run validates the fixtures and parser without
calling a model:

```sh
/usr/bin/python3 router_eval.py
```

The optional live run sends the same cases through isolated Codex sessions,
without changing the bot's durable router session:

```sh
/usr/bin/python3 router_eval.py --live
```

Pass `--model MODEL` to compare a candidate model. A router or model change is
ready only when all cases pass. The default model passed 6/6 on July 23, 2026,
then 8/8 after aliases and 9/9 after model/effort configuration was added.

The earlier strict `route`, `clarify`, or `reject` schema remains as a compact
classification and evaluation primitive. It is not intended to become a
user-facing command language.

For controlled debugging, each loop can run separately:

```sh
./telegram_control.py collect
./telegram_control.py work
./telegram_control.py work-router
./telegram_control.py work-agents
./telegram_control.py send-outbox
```

Use `--once` on any individual loop to handle at most one polling request or
queued item. Dead items are explicitly requeued with
`telegram_control.py retry inbox`, `telegram_control.py retry router`, or the
corresponding agent/outbox queue name.

## Durable reply continuity

When the main router dispatches a root Control request to a managed project
agent, the dispatch preview and then the agent's final response replace the
root `🧭 Routing…` receipt in place. That single card carries durable reply
continuity:

- Once Telegram acknowledges the dispatch-preview edit, the receipt's stored
  reply route
  is retargeted, in the same completing transaction, from the main router to
  the exact project agent that owns the now-running turn. This makes the live
  card steerable before the final answer exists. Route ownership never switches
  before the visible dispatch acknowledgment; the transition is idempotent,
  crash-safe, and scoped to the exact `(chat, topic, message)`.
- Replying to that edited final message therefore continues the same managed
  agent conversation—using its persisted provider session—directly from the
  root Control chat. A new turn gets its own `📨 Queued…` receipt in the
  Control chat, the response arrives as a new routed message, and the receipt
  is deleted only after that response succeeds, so follow-up replies chain
  indefinitely.
- The reply route is revalidated durably at enqueue time: a reply from the
  wrong chat or topic, to the wrong message, after route expiry, or against a
  different agent fails closed.
- If Telegram rejects a direct agent final send, the response retries and its
  progress receipt remains visible. The receipt cleanup is not created until
  the last final chunk has been acknowledged, so nothing is lost.
- Delivery order is enforced, not hoped for: every sender process wraps the
  actual Telegram call in one exclusive, non-reentrant kernel advisory lock
  (an owner-only flock file derived from the canonically resolved controller
  database path, single-host by design), atomically revalidating and
  renewing its lease — sized to outlive the API call's hard 180-second
  whole-operation deadline, enforced by running every Telegram request in a
  killable helper subprocess: killing the child bounds every phase of the
  operation, including macOS DNS resolution, which no in-process signal or
  socket teardown can reliably interrupt. The helper inherits the sender's
  locked delivery descriptor (`pass_fds`), so the kernel keeps the flock
  held until the helper itself exits — even if the sender is SIGKILLed
  mid-call or the helper resumes from macOS sleep before any of its threads
  are scheduled — which makes inherited lock ownership the ordering
  guarantee; the helper's self-termination (wall-clock deadline or parent
  death) is cleanup, not the proof. The bot token reaches the helper on
  stdin, never in process arguments, reflected error descriptions are
  token-redacted, and standard urllib proxy handling is retained — and
  re-checking
  supersession inside the critical section before calling Telegram, then
  recording the durable outcome before releasing. A retried edit answered
  with “message is not modified” (a lost acknowledgment) completes normally
  so the retarget still runs, a stale `Waiting for the agent…` preview edit
  is superseded durably once the agent-outcome edit exists, and edits of the
  same routing receipt carry a typed serialization key so they are never
  claimed concurrently. Two residual windows remain and both degrade to the
  documented at-least-once duplicate-delivery semantics, never to
  reordering: a sender process dying — and the deadline expiring — at an
  instant when Telegram had accepted but not yet applied a request. Neither
  can be fenced without server-side compare-and-swap.
- Multi-chunk responses send every chunk as a routed final message in order.
  Acknowledgment of the last chunk queues deletion of the `⏳ Working…`
  receipt, so cleanup cannot run after only a partial response.
- Voice replies follow the same durable routes as text replies. A voice note
  recorded as a reply to a retargeted agent answer continues that exact agent
  and persisted provider session: the reply surface gets the usual
  `🎙️ Transcribing…` receipt, the local transcript becomes the agent input,
  the agent's final response arrives as a new message, and the receipt is then
  removed. The stored route is revalidated durably before any mailbox work is
  created, so foreign, stale, or mismatched voice replies fail closed exactly
  like text.
- Completed managed-agent answers include a one-time
  **🔊 Listen via Microsoft TTS** button.
  It synthesizes that already-stored answer on demand with `edge-tts`, encodes
  it as a bounded OGG/Opus Telegram voice note, and keeps the complete text as
  the canonical fallback. The voice note and its **Replay** button are bound
  to the same authorized user, chat, topic, and durable agent reply route.
  Generation is network-dependent and sends the bounded speakable text to
  Microsoft’s Edge TTS service only after the owner taps the button; a failure
  leaves the text untouched and offers **Try voice again**. Private temporary
  speech files are published atomically and removed only after confirmed
  delivery or terminal failure. Stale cleanup consults queued and leased
  outbox references before removing completed audio.

Managed Codex and Claude turns can also send an update before their final
answer through the implicitly triggered `telegram-voice-message` and
`telegram-text-update` skills. Natural requests such as “send me a voice
message when you finish” or “keep me updated in Telegram” are sufficient; the
user does not need to name a skill. The provider-neutral `agent_telegram.py`
helper accepts concise text or voice on standard input, verifies the caller's
active mailbox lease, resolves that turn's owning Telegram topic, and writes
to the durable outbox. It does not receive the Telegram bot token. Stable
caller-provided keys and a content hash make retries idempotent. Canonical
skill definitions live under `skills/` and are installed in both the Codex and
Claude user skill directories. Voice messages send their bounded text to
Microsoft Edge TTS.

The repo also owns the implicitly triggered `telegram-group-icon` skill.
Inside an active managed turn it can create or select a square PNG/JPEG and
apply it only to the group that owns that turn; the scoped helper accepts no
chat ID, refuses private chats, revalidates the live mailbox lease, and checks
the bot's **Change group info** administrator permission before changing the
photo. The Telegram bot token remains in macOS Keychain.

The `telegram-topic-teardown` skill gives an active managed topic a formal,
owner-confirmed shutdown path. A natural request to tear down the current
topic asks the scoped helper to post a permanent-deletion confirmation card;
the direct `/teardown` command opens the same card without an agent turn.
Confirmation refuses active turns, running consoles, and detached workers that
still report to the topic. Once idle, it archives the managed agent and
subject, revokes routes and callbacks, frees the project slug for reuse, and
durably queues deletion of the exact Telegram topic. The provider performs no
direct database, tmux, or Bot API deletion.

Install or refresh repo-owned shared skills without reinstalling the
controller:

```sh
./telegram_control.py install-skills
```

The version-controlled source remains under `skills/`. Installation copies
the runtime skill into `~/.agents/skills/`, which Codex discovers directly,
and creates the per-skill relative Claude link under `~/.claude/skills/`.
Because skill metadata is loaded when a provider session starts, restart an
existing Codex or Claude session before expecting a newly installed skill to
appear.

### Agent-created conversational topics

The implicitly triggered `telegram-create-topic` skill lets any active managed
topic create another ordinary conversational topic in its own bound private
forum. This is deliberately separate from `telegram-detached-worker`: the new
topic has a normal managed-agent mailbox, accepts user messages directly, and
starts an independent provider conversation rather than a report-only tmux
worker.

`agent_telegram.py topic-create` accepts a stable key and topic name, plus
optional provider, model, and effort overrides. Omitting them inherits the
forum workspace's exact defaults. Standard input is optional; when present, it
is a self-contained first prompt that is queued immediately after the topic is
attached, so the new session can begin while the originating turn is still
running. The provider session itself remains lazy until that mailbox item is
claimed. Empty standard input creates a ready-to-chat topic without starting a
provider.

The helper accepts no Telegram coordinates. It validates the originating
mailbox lease, follows the agent's durable home binding rather than any
temporary reply route, requires an active bound forum, resolves the authorized
owner from the source inbox job, and checks the bot's **Manage topics**
administrator permission immediately before `createForumTopic`. The durable
subject stores the creation operation and canonical plan digest in its existing
memory JSON. Replaying the same key and plan reuses the attached topic; a
different key, prompt, provider configuration, or pre-existing topic with the
same name fails closed.

First prompts preserve the existing agent-mailbox provenance invariant without
a schema change. A deterministic negative update ID — a namespace Telegram
updates never use — records a completed internal inbox source whose payload
contains the owner, target topic, exact prompt, and originating operation.
`source_inbox_job_id` therefore remains a real foreign key, stop controls
remain owner-bound, and ordinary receipt, progress, steering, response, and
retry paths need no special case. The opening topic-intro card uses the already
supported `topic_intro` outbox kind, so no new mixed-version sender payload was
introduced and no controller restart is required.

Verified with a helper-level integration test that creates a bound forum and
leased originating agent, mocks the Bot API permission and topic-creation
calls, asserts inherited provider settings, the managed subject and route,
opening intro card, negative internal source job, and queued first prompt, then
replays the same request and proves that neither the Telegram mutation nor the
mailbox turn is duplicated. The final repository-wide run passed all 359 tests
on July 27, 2026.

### Restarting the controller safely

This subsection originally documented the delayed `restart` helper. It is
superseded by the database-backed idle-restart request described later in
[Restarts are queued, not timed](#restarts-are-queued-not-timed). For a live
controller, use:

```bash
./telegram_control.py request-restart --reason "Apply updated worker code"
```

The supervisor claims the request only when inbox, router, agent, and outbox
leases are all idle, then exits so launchd's existing `KeepAlive` policy loads
the new code. The older `restart` command remains for compatibility, but it is
not the documented live-update path. Do not use `launchctl submit` directly for
controller reloads.

## Live Codex worker control

Schema v17 persists the provider turn ID separately from the provider session
ID and adds a durable control queue for active worker turns:

- A turn card progresses from `📨 Queued` through bounded Codex-authored
  or Claude-authored user-facing updates and exposes a one-time `⏹ Stop`
  button while work is active.
- Replying to the exact active turn card queues guidance for that exact
  provider turn. Text and transcribed voice replies use the same durable
  control path; only the Codex or Claude adapter that transports the steer is
  provider-specific. Replying to any other agent message starts an ordinary
  follow-up turn instead.
- Stop is durable before and after the provider turn ID becomes available. It
  is delivered through app-server `turn/interrupt`; a cancellation is terminal
  and is never retried as a failed turn. If Stop can prove that a standard
  same-host lease owner process no longer exists, it atomically clears that
  orphaned turn instead of waiting for lease expiry; ambiguous, remote, or
  permission-denied process checks retain the durable interrupt path. Clearing
  releases the per-agent mailbox immediately so the next queued message can
  start, and the terminal card says that the worker exited rather than claiming
  that the provider acknowledged an interrupt. The clear is recorded as an
  `agent_turn_orphan_cleared` event with the mailbox, agent, and former lease
  owner. Verified on July 26, 2026 with regression tests for a dead same-host
  worker immediately releasing the next queued turn, a live same-host worker
  retaining the native Stop path, and all three existing crash/Stop recovery
  states.
- Controls are tied to the active mailbox lease and expected provider turn.
  Lease recovery rejects uncertain in-flight controls instead of replaying
  them against a replacement turn.
- Status, terminal, voice, and router-card edits share per-turn serialization
  keys. Terminal completion supersedes queued status edits and removes the
  inline keyboard, preventing an old `Working` or `Stop` state from
  overwriting a final response.

### Agent worker exits are isolated and reconciled

The Reservations incident on July 27, 2026 exposed three interacting failure
paths. A manual rescue correctly closed one stale lease, then used the global
`retry agent` command. That requeued two unrelated dead turns from July 24 and
reset their attempt counters. One replay therefore reused
`agent-mailbox:42:retry-1` after status-card formatting had changed; the
outbox's strict idempotency check correctly rejected the changed payload. The
error escaped the agent worker, and the supervisor's original all-or-nothing
policy terminated every sibling worker. Two newly leased Reservations turns
were left owned by dead worker PIDs. Claude's provider CLI runs in its own
process group, so one provider survived as useful but untrackable orphaned
work, while the other turn had only a stale database lease.

The repair has four layers:

- Provider adapters report their process-group leader immediately after
  launch. The durable event log ties that PID to the exact mailbox lease and
  worker owner without a schema change.
- Each supervised child is now its own process-group leader. An unexpected
  child exit terminates the provider process groups recorded for that worker,
  releases only that worker's leases, and restarts only that child. Other
  active topics continue uninterrupted. Supervisor startup performs the same
  reconciliation for same-host lease owners proven to be gone.
- Manual agent retries preserve the monotonic attempt counter, so retry status
  and failure operations receive a fresh attempt namespace. The CLI requires
  either `retry agent --id MAILBOX_ID` or an explicit `retry agent --all`, and
  stopped agents are not resurrected by a bulk retry.
- Agent heartbeats extend the Stop callback beyond the renewed worker lease.
  If an old Stop callback is tapped while its mailbox is still queued or
  leased, callback consumption renews it instead of reporting a false expiry.
  Terminal recovery edits remove the keyboard normally.

The live repair left the working Postmortem provider untouched, cleared the
dead Watch Monday lease and the two unrelated replays, expired their callback
actions, and queued terminal Telegram edits. Three visible stale keyboards
were removed; the fourth target message was already absent in Telegram.

Verified on July 27, 2026 with 403 passing tests, including new coverage for
monotonic manual retry operation IDs, long-turn Stop renewal, consumption of an
expired-but-still-live Stop callback, exact worker/provider PID recording,
single-worker lease recovery, explicit agent retry scope, and supervised
process-group creation.

## One bot across private forum groups

Slam Paws is the single Telegram identity. The paired owner may add it as an
administrator to private forum groups without creating another BotFather token
or controller process. Administrator status matters: Telegram's default Group
Privacy hides ordinary topic text and voice from non-admin bots. The
transport accepts an update only when:

- it comes from the original paired user;
- it is in the paired private bot chat or a private forum supergroup topic
  without a public username; and
- every later callback still matches its stored chat, topic, and authorized
  user.

Messages from other group members, non-forum groups, public groups, channels,
and unrelated private chats are discarded before their content enters SQLite.
A first text or voice message in a new private forum normally produces one
owner/topic-bound **Authorize forum** button and does not reach Control. A
first text message that explicitly asks to set up or bind the forum and
includes a discoverable local path instead offers one combined
**Authorize and bind** confirmation. The callback revalidates the path and
atomically creates both the Control surface and forum workspace; there is no
resend between those steps. Consequential topic-agent creation remains lazy
and separately bounded. Multiple bot tokens are not required.

An authorized forum can then be bound conversationally to one existing local
workspace, including a non-Git notes tree. Control resolves the user-stated
path or an enrolled project, validates the workspace and optional working
directory, and presents a one-time **Bind forum workspace** confirmation. The
durable forum record stores the realpath-resolved boundary, optional exact Git
root, and Codex model/effort defaults. Repeating the same confirmation is
idempotent; silently rebinding an active forum to another directory is
rejected.

After a forum is bound, Telegram's topic-creation service message prompts the
owner to choose Codex or Claude, then a provider-specific model and effort.
Each model and effort menu starts with **Default**. Setup confirmations and
`/agent` status resolve a Default selection to the concrete value currently
inherited from the local Codex or Claude configuration, while retaining the
Default label. The final choice atomically creates one durable subject, one
topic route, and one managed worker without starting a provider session; the
first ordinary text or voice request starts that session. If the creation
service update was missed, `/agent` or the first ordinary request presents the
same chooser, and an already-sent request must be resent after setup. Later
messages reuse that subject and its persisted provider session; topic renames
update the user-facing subject label without changing its identity. The worker
inherits the forum's exact workspace boundary, working directory, and optional
Git metadata, plus the selected provider configuration. Existing managed agent
mailboxes provide receipts,
editable progress, provider-neutral text/voice reply-to-steer, Stop,
pause/resume, and explicit new-session controls—no new daemon or actor runtime
is introduced. Read-only commands such as `/status` do not create a subject,
and exact replies to earlier Control messages keep their durable Control route
for both text and voice even after the topic becomes a subject.

Live setup:

1. Create a private Telegram group and enable Topics.
2. Add Slam Paws and promote it to administrator so Group Privacy does not
   suppress ordinary text and voice.
3. In General, send `Set up this group for /absolute/workspace/path using
   Codex` (or `Claude`) and tap **Authorize and bind**. If the first message
   does not include an explicit setup request and path, use the existing
   authorize-then-bind flow instead.
4. Create a topic and use its automatic buttons to choose the provider, model,
   and effort. Then send an ordinary request to start the selected provider
   session; later requests continue it. Send `/status` in that topic to inspect
   or control its managed agent.

For immediate clean removal, send `/teardown` in the managed topic and confirm
its Telegram card. If a topic is instead deleted directly in Telegram, the
durable supervisor's `maintain-topics` loop repairs the stale state on its next
check, at most once per day. The Bot API has no read-only topic lookup, so the
check sends one silent invisible message and deletes it immediately.
Telegram's explicit `message thread not found` (or equivalent invalid-topic
response) atomically revokes the topic route, callbacks, reply routes, and
status card, archives the forum subject and managed agent, frees the original
slug, and forgets the controller's resumable provider-session
pointer. Historical inbox/outbox and event rows remain as a compact audit
trail. Network failures, permission errors, closed topics, and all ambiguous
responses leave the binding active and retry on a later cycle; active agent
work or a running console also defers cleanup. A crash in the narrow interval
between the probe send and delete may leave an empty, silent message, which is
recorded for diagnosis rather than risking deletion of a valid topic. The
maintenance process starts and restarts with the same LaunchAgent-backed
supervisor as the rest of Telegram Control. For a one-time diagnostic run:

```sh
/usr/bin/python3 telegram_control.py maintain-topics --once
```

Replies that legitimately remain with the main router (receipts, direct
responses, relayed continuation chunks, fallback deliveries) now carry bounded
reply context, for voice replies exactly as for text; voice status edits
display only the user's transcript, never the composed context. The durable router input embeds up to 1,000 characters of the
replied-to bot message between explicit delimiters, plus a provenance label
derived from the durable outbox operation that produced the message—never from
message content. Deictic follow-ups such as “why did that happen?” therefore
reach the router with trustworthy context. Quoted text is explicitly marked as
data, delimiter spoofing inside the quote is stripped, stored filesystem paths
and secrets are never included, and controller validations that require a
value to appear explicitly in the user's request ignore the quoted context
entirely—including when a clarification button resumes the original request.

Quoted context is additionally fenced by a deterministic dispatch guard: on a
reply-context turn, `send_to_agent` executes directly only when the
user-authored reply itself names the destination by canonical slug, display
name, or durable alias. Otherwise the controller converts the model's
selection into a one-time authorized confirmation question (`Yes, send it` /
`No, cancel`) instead of dispatching, so instructions hidden in quoted bot
text can never reach a project agent without an explicit user action.
Consequential tools keep their existing controller-enforced confirmations.

## Conversational Control agent

The main Control chat is no longer a one-shot intent classifier. Each turn is
a bounded, durable multi-step investigation:

- Control can call two read-only discovery tools — `find_directory` and
  `inspect_directory` — as many as six times per turn before committing to
  exactly one terminal outcome. Discovery is confined to the configured
  discovery roots (`discovery_roots` in the bridge config, defaulting to your
  home directory), skips hidden directories, follows symlinks only inside the
  roots, and reports Git-root status and subdirectories with hard result
  caps. Completed steps persist on the turn, so a crash-recovery retry
  resumes from recorded history, and step/time/path bounds end a runaway
  investigation with a precise message.
- Every discovered directory receives a controller-issued opaque ref ID.
  A creation proposal may identify its workspace root and working directory
  only by those refs or by text you yourself wrote; a model-invented path or
  forged ref fails closed, and confirmation payloads carry full
  `{value, source, derived_from}` provenance.
- Managed agents attach to an arbitrary existing workspace directory; Git is
  optional metadata, not an enrollment requirement. The agent runs
  (structured turns and tmux console alike) in a working directory that must
  stay inside the confirmed workspace through symlink-resolved containment
  checks at proposal, confirmation, and every launch. A notes directory such
  as `~/life` is therefore a valid workspace. If the workspace itself is a Git
  root, branch and cleanliness metadata remain available without widening the
  authorized boundary to a containing parent repository.
- Mutations stay confirmation-gated behind authorized one-time buttons, now
  including agent model/effort configuration changes. So a request like
  “add a project called Lovely, the Peter app subdirectory of the lovely
  repo in software inside my user directory” resolves the repository and
  `peter-app` working directory by discovery and produces a validated,
  confirmation-backed proposal — without requiring you to type absolute
  paths. Schema v15 additionally records project-topic creation and topic
  renaming as durable mutation sagas. A compare-and-set claim gives exactly
  one concurrent confirmation permission to cross the Telegram API boundary;
  durable external results resume local application after a crash, while an
  ambiguous lost API result enters explicit reconciliation instead of
  repeating a possibly successful Telegram mutation.
- Every Control-chat turn identifies its speaker: `🎛 Control` for the
  Control agent, `🎛 Control → Project` on dispatch handoffs, and the
  project's name on relayed or reply-continued agent responses. The old
  “Router preview / Would inspect…” fallbacks are gone; responses state
  exactly what was done or found, and topics still bound to the controller
  converse with Control directly.

## Durable inbound attachments

Authorized Telegram photos and arbitrary documents up to 20 MB are valid
agent input. The inbox handler selects Telegram's largest photo rendition or
accepts any document regardless of MIME type, downloads it
through the token-isolating bridge, and atomically publishes it at a private
mode-0600 path under the database sibling `attachments/inbox-<job-id>/`
directory. The job ID and Telegram `file_unique_id` make the path stable across
inbox retries, and incomplete `.part` files are removed before a retry.

The normalized prompt contains the absolute attachment path and optional
Telegram caption, then follows the same durable routing, reply continuation,
and active-turn steering paths as text. This is deliberately provider-neutral:
Codex and Claude receive ordinary text pointing to a local file they can
inspect with their built-in filesystem/image tools. The full Telegram update
was already persisted before the download, so this feature requires no schema
migration.

Before making Telegram's `getFile` request or downloading any bytes, the inbox
handler queues the eventual router or agent turn receipt with
`📎 Attachment received. Downloading securely…`. Routing later reuses that
operation ID and card instead of sending a second message, so the independent
sender can acknowledge a slow attachment immediately and the same Telegram
message becomes the normal progress and response card.

Verified with unit coverage for Telegram rendition selection, arbitrary
documents and safe filename retention, deterministic private persistence,
retry reuse, acknowledgement-before-download ordering, prompt construction,
single-receipt reuse, and the focused attachment suite.

## Fewer steps to a working group

Setting up **Meet Without Fear** on July 25, 2026 took eleven Control messages
and seven taps before any work ran, and asked the owner to send the same request
twice. The durable record of that setup drove four changes.

**A new group is asked for its folder instead of waiting to be told.** The
authorize card and the post-authorize confirmation both end with the same
question (`WORKSPACE_QUESTION` in `on_message.py`), because the workspace is the
one thing the controller cannot infer. The card only asks it when the forum is
still unbound; a re-authorized bound forum keeps the old "send your request
again" wording.

**A message in an authorized, unbound forum is framed as that answer.**
`compose_forum_setup_input` prefixes a controller-authored note saying the owner
was just asked which folder to use, and that the message below answers it. The
note never contains a path, and it is separated by the same marker convention as
reply context, so `extract_user_request` still returns only the user's own
words — path, alias, model, and effort containment checks are unchanged. Unlike
quoted bot text, this framing is trusted controller output, so
`has_reply_context` explicitly reports `False` for it and the reply-context
dispatch guard does not misfire. A bare description such as `the meet without
fear repo in Software` therefore reaches Control as a bind request, and
discovery resolves it to a ref before the usual confirmation.

**A topic starts with its group's provider in one tap.** `bind_forum_workspace`
already records the provider and any model/effort defaults, and
`ensure_forum_subject` already inherits them, so asking provider → model →
effort again re-asked a question the binding had answered. The card now states
the inherited configuration and offers **▶️ Start \<provider\>**, with
**Choose a different agent…** opening the previous three menus for a topic that
should differ from its group. The start action re-reads the forum record at
confirmation time rather than trusting its stored payload, so a stored button
can never widen the provider it was issued against. `bind_forum_workspace` also
accepts `claude` now; restricting it to Codex made the recorded default
misleading for a Claude group.

**An interrupted request is held, not discarded.** Setup carries the pending
text (bounded at 4,000 characters) through the start, customize, provider,
model, and effort payloads, and enqueues it to the new agent's mailbox as the
topic's first turn once the subject exists. Voice notes are transcribed before
the setup card appears so the transcript can be carried the same way — a few
seconds of transcription is cheaper than asking someone to re-record. An
attachment's generated prompt carries identically, so an image sent into a fresh
topic also survives setup.

Verified with the reworked topic-creation integration test (default card, the
customize path, and per-topic Claude/sonnet/high still landing on the agent),
a new one-tap test asserting the held request becomes the first mailbox item and
that no setup buttons stay active, a new framing test asserting the router input
starts with the controller note while `extract_user_request` returns only the
owner's sentence, the full 331-test suite, and the offline router eval gate at
14/14.

## Reboot recovery for detached workers

Schemas v22–23 turn a detached worker into a durable logical session rather than
equating it with its tmux process. The worker row now stores the exact provider
session ID, provider configuration, working directory, durable recovery-file
path, recovery prompt, generation, handshake state, timestamps, and last
failure. Claude sessions receive a harness-assigned UUID at launch. Codex
sessions choose their own ID, so startup snapshots the exact-directory session
set and persists the one new ID before `worker-start` succeeds.

Every worker receives a private
`<database directory>/detached-workers/<name>/RECOVERY.md` and a standing
provider prompt before its task brief. The prompt requires the worker to keep
that file sufficient to restore active goals, provider-native wakeups and
scheduled tasks, background agents, monitors, processes, durable identifiers,
verification steps, and idempotency constraints. It explicitly preserves the
provider's native scheduling and teamwork mechanisms; Telegram Control does not
become a task scheduler.

The supervised `maintain-workers` loop compares durable intent with exact tmux
existence. An intended-running worker whose tmux session vanished is resumed
with the provider adapter's ordinary persisted-session command. The injected
recovery turn tells the same conversation to read its recovery inventory,
reconcile current external state, reactivate its own native background work,
and call a generation-bound success or failure command. Recovery remains
`starting` until that explicit confirmation. Started, verified, failed,
timed-out, missing-session, and exhausted-retry messages are written through
the durable outbox and serialized per worker. Attempts back off, time out after
30 minutes without confirmation, and stop automatically after three failed
launches while preserving the session record for manual repair.

Existing pre-v22 workers can be attached to a known exact-directory provider
conversation with `worker-adopt-session`; ordinary new workers persist the ID
automatically. `worker-status` exposes whether the session ID and recovery file
are present, the handshake state, and the last recovery error. `worker-stop`
removes the exact harness-owned `RECOVERY.md` along with the worker row, but
will not recursively delete unexpected companion files; the report topic still
follows the existing explicit `--delete-topic` choice.

The live controller opened schema v22 during the mixed-version development
window before `recovery_file_path` had landed in that migration. Schema v23 is
a conditional additive repair: it inspects `detached_workers`, adds only the
missing column, and is a no-op for clean v22 databases that already have it.
This exact partial-v22 shape has a migration regression test.

Verified with store tests for recovery metadata and stale-generation rejection,
provider launch tests for Claude's preassigned session identity, recovery-file
contract and teardown tests, and lifecycle tests covering exact-session
relaunch, explicit agent confirmation, durable success/start reporting, brief
submission ordering, and safe refusal when no session ID exists. The final full
suite passed at 345 tests and the offline router gate passed at 14/14.

Live activation adopted the existing reservations `release-monitor` Claude
conversation, created its durable recovery directory, and had the worker write
and reread a 250-plus-line inventory covering its two native wakeups, independent
backstop, runbook and analyzer copies, completed side effects, time-dependent
recovery rules, and verification criteria. The original tmux session was then
killed while its independent backstop remained alive. One reconciliation cycle
resumed the exact Claude session and delivered the recovery prompt. Claude read
the inventory, found both original wakeup IDs still present, avoided duplicates,
verified scheduler liveness with a disposable native wakeup, checked the
backstop, Git, production revision, and artifacts, updated the inventory with
what the test proved, and invoked the generation-1 success handshake. SQLite
ended at `recovery_state=succeeded`, `observed_state=running`, and retry count
zero; both the started and verified-success outbox messages reached `sent`.

### The contract is a launch prompt, not a per-brief header

`worker-brief` used to prepend the entire recovery contract to every brief it
delivered, including relays of the owner's follow-up instructions. Reading the
`journey-story` transcript showed what that cost over a seven-hour run: seven of
its thirty-seven messages carried the full block, and each one closed with "Wait
for the task brief after applying this contract" sitting directly above the
brief it was telling the worker to wait for. The same transcript ruled out the
worse failure mode — the repeats did not make the worker re-read `RECOVERY.md`,
which it opened three times all night.

The token cost was never the point; roughly 290 tokens a repeat is noise in a
200k window, and none of it deduplicates, since each copy extends the
conversation prefix and is billed once at full input price before becoming a
cache read. The cost was salience. Repetition is how a model decides what
matters, so restating the bookkeeping contract seven times weighted it against
the actual work.

`create_worker` still appends the full contract to the launch command, so every
brief that reaches `worker_brief_command` is necessarily after it. Briefs now
carry `recovery_file_reminder` instead: two lines naming the worker and its
recovery-file path, ending by pointing at the instruction below rather than
asking the worker to wait for it. Verified with the 29 detached-worker tests.

### Then the premise turned out to be wrong

The contract existed to answer a question nobody had tested: what does a resumed
worker come back knowing? The assumption was "nothing" — hence an inventory the
worker hand-maintained so it could rebuild itself from notes.

A direct experiment says otherwise. A throwaway Claude session was launched with
a fixed `--session-id`, told to schedule a recurring job, and killed at the tmux
level. Resuming that session ID and calling `CronList` returned the job, and the
raw transcript confirms the call was made by the new process rather than
summarized out of replayed history. A fresh session in the same directory
returned nothing, ruling out a global scheduler. `CronCreate`'s own description
("dies when Claude exits") describes process exit — accurate for `--print`
turns, wrong for a resumed interactive session.

So the inventory was a hand-kept copy of state the harness already restores, and
maintaining it was the expensive half of the design: `journey-story` grew a
14.7 KB `RECOVERY.md` rewritten by shell heredoc roughly twenty times a session,
an order of magnitude more context than the contract repeats ever cost.

`recovery_file_contract` and `recovery_file_reminder` are therefore gone.
`launch_preamble` tells a worker only that it is detached and should keep using
its native scheduling, wakeup, loop, and background features; briefs are
delivered verbatim; and `DEFAULT_RECOVERY_PROMPT` asks the resumed worker to
check its scheduled work is still active, recreate what is missing, and confirm.

The durable-store plumbing is deliberately untouched — `recovery_file_path`,
generations, and the confirm/fail handshake all stay. Removing them means a
schema migration on a live system for no behavioral gain, so the columns simply
go quiet and `RECOVERY.md` is created but never mentioned to the worker.

## Agent-authored questions with buttons

Agents could send text, voice, group icons, detached workers, and teardown
cards, but they could not ask a question with choices — only Control could,
through `ask_user`. The `telegram-ask-owner` skill closes that gap with
`agent_telegram.py ask --key <key> --option <label> …`, question on standard
input.

A managed turn is one-shot, so the answer cannot return to the process that
asked. `enqueue_agent_choice_prompt` therefore mirrors the router's
clarification behavior: it validates the live mailbox lease, requires the
agent's own topic, resolves the authorized user from the originating inbox job,
and creates one opaque one-time callback action per option, all inside one
transaction with the outbox message that carries the keyboard. Tapping an option
expires its siblings and queues a **new turn for the same agent** containing the
question and the chosen label, with the ordinary `📨 Queued` receipt. Options are
bounded at 2–5 distinct plain-text labels of at most 64 characters, the question
at 1,000; the callback data still carries only a token.

The skill instructs agents to ask and then finish the current turn rather than
waiting, because nothing will arrive before the process is torn down.

Verified with a new test that runs the helper against a leased turn, asserts the
queued keyboard and one-time owner-bound actions, taps the second option through
`handle_callback`, and asserts the answering mailbox turn plus the expired
sibling. Full suite green at 333 tests.

## One tap to add the bot to a new group

Standing up a project group meant five manual steps: create the group, enable
Topics, add the bot, promote it to administrator, then create a topic. Telegram's
Bot API can remove exactly one of them — a bot cannot create a group, add
itself, promote itself, or toggle forum mode, and no method exists for any of
that. Only a user-account (MTProto) client could, which was considered and
deliberately not built: it would add the project's first third-party dependency
and a stored session equivalent to full account access.

What is possible is collapsing *add* and *promote* into one confirmation.
`/newgroup` replies with a card whose inline URL button is
`https://t.me/<bot>?startgroup=true&admin=change_info+delete_messages+manage_topics`,
built by `bridge.group_setup_link` from the paired `bot_username`. Telegram then
adds the bot to the chosen group and asks the owner to grant those three rights
in the same step. The rights are the ones actually used: admin status at all is
what defeats Group Privacy, `change_info` sets the group icon,
`delete_messages` retires progress cards, and `manage_topics` creates and
deletes managed and worker topics. An unusable or missing username fails closed
rather than producing a broken link.

Telegram delivers `/start <payload>` into the group once the link finishes
adding the bot. That is an arrival rather than a request, so `/start` in a
supergroup only ever runs the authorization prompt — which now also asks which
folder the group works in — and is never routed to Control as work. The card is
explicit that creating the group and enabling Topics remain manual.

The handler also stopped re-reading `config.json` to name the bot: the worker
passes `TELEGRAM_BOT_USERNAME` in the handler environment, with the config as a
fallback, so user-facing copy and the link share one source of truth. Control's
prompt now states that a bot cannot create a group and that the owner should be
pointed at `/newgroup`, so a request for a separate group is no longer answered
with a private-chat topic.

Verified with unit coverage for the link's exact rights and its fail-closed
paths, and an integration test asserting `/newgroup` queues the URL button
without creating a router turn while `/start true` in an unauthorized forum
produces the authorize-and-folder prompt and no router work. Full suite green at
336 tests; offline router eval 14/14.

## Every topic opens with one intro message

Telegram pins nothing on its own, and the controller never pinned anything
either — the only pin in the whole durable record was made by hand from the
owner's account, which is why topics looked inconsistent. A topic's commands
were reachable only by scrolling back to whichever message last mentioned
`/help`.

Each topic now opens with one intro that states the agent, model, effort, and
context, and lists the commands. It replaces the previous confirmation rather
than adding a message, and it is sent on both setup paths — the one-tap start and
the per-topic customize chain — including when a carried request runs
immediately, where it says so instead of asking for a first message.

That message was briefly pinned as well, and the pinning was then removed: the
registered command menu (below) makes every command reachable from the compose
field in every chat and topic, which is what pinning was for. `pinChatMessage`,
`unpinAllForumTopicMessages`, the `pin_messages` right in the `/newgroup` link,
and the sender's pin-specific failure handling are all gone. What remains of the
mechanism is the part that earns its keep: the intro carries a `topic_intro`
card, and the outbox completion transaction records the message ID Telegram
assigned so later turns can edit that same message in place.

Verified with an integration test that drives topic creation through the one-tap
start, asserts the intro lists the commands and carries the record card, drains
the outbox, and asserts that no pin or unpin call is queued while the
acknowledged message ID is stored on the subject.

### What the first live rollout broke

The first topic to reach this code (**Prompt improvements**, July 26, 2026)
exposed a mixed-version fault, not a pinning fault. The sender still held code
from before the commit, claimed the intro row, delivered it to Telegram, then
could not interpret its unfamiliar `topic_intro` card kind and raised
`Outbox surface card metadata is invalid.` The child exited, the supervisor
restarted the whole controller two seconds after the intro was queued, and the
orphaned lease held the row until it expired ten minutes later — at which point
the new sender redelivered the intro (so that topic has two copies of it) and
pinned it — the one pin this feature ever performed, since removed.

`complete_outbox` now treats an unrecognized card `kind` as "no follow-up work I
know about": it records the delivery and returns, instead of raising. Known kinds
with malformed fields still fail closed. A regression test completes a row
carrying an invented kind and asserts the row reaches `sent`.

The same rollout also proved that editing `on_message.py` is a live deploy: a
mid-refactor save left the file unparseable and three turns died with
`IndentationError` before the next save. Both hazards are now recorded in
`CLAUDE.md`.

### A command menu, which replaced pinning entirely

Telegram has a native affordance that is simply better: `setMyCommands` puts
every command in the compose field's menu, in every chat and topic, with no pin,
no scrolling, and no admin right. `telegram_help.COMMANDS` is now the single
source of truth — the `/help` home page is rendered from it — and
`telegram_control.py sync-commands` publishes it. `install` runs it too, warning
rather than failing if the network call does not land. A test asserts the
registered list matches the help copy and Telegram's name and description limits.
Registered live and confirmed with `getMyCommands` on July 26, 2026.

### The intro header is live, not a snapshot

A message that states a model, an effort, and a context measurement is only
useful if those stay true, so the intro is edited in place rather than left as a
record of the moment a topic was created.

`topic_intro_text` in `durable_store.py` is the single renderer for that message
— provider, model, effort, the current session's context snapshot (or "no turn
completed yet"), a paused marker when the agent is paused, and the command list —
so the message a topic is created with and every later refresh cannot drift.
`record_topic_intro_message` stores the intro message's ID in the forum
subject's existing `memory_json`, which deliberately avoids a migration:
`surface_cards.card_type` is constrained by `CHECK (card_type IN ('status'))`, and
a schema bump is exactly what breaks daemons still running older code.

`enqueue_topic_intro_refresh` re-renders, compares a hash of the text against the
stored `intro_revision`, and enqueues an `editMessageText` only when something
actually changed — a quiet turn costs no Telegram call and never provokes
"message is not modified". Refreshes carry their own `topic-intro:<chat>:<topic>`
serialization key so they never queue behind, or ahead of, a live turn card. It
runs in the same transaction that records a completed turn's usage, and on
model/effort reconfiguration, pause, resume, and new-session reset.

The refresh edit carries no `card_json` on purpose. An older sender interprets
`topic_intro` cards strictly enough to reject a non-`sendMessage` method, so a
carded edit would have crashed the very daemons this rollout has to survive.

Verified with a test that leases a real forum-subject turn, completes it with
provider-reported context, and asserts the queued edit targets the intro message
ID, reports `41% used · 82,500 / 200,000 tokens`, keeps the commands, uses the
intro serialization key, and enqueues nothing at all on a second unchanged
refresh. Full suite green at 350 tests.

Until the daemons restart, a new topic still gets its intro but no refreshes:
recording the message ID and queueing edits both live in worker code that the
running processes predate.

## The surface is the label

Every agent-authored message repeated the topic's own name — `📨 Queued for
Prompt improvements`, `🎙️ Prompt improvements is transcribing…`, and the name
above each answer — while Telegram already shows that name at the top of the
screen. The label is now conditional on where the message lands.

`agent_surface_header(agent_id, chat_id, message_thread_id)` returns an empty
string when those coordinates are the agent's own bound surface, and the durable
project name otherwise; `label_text` joins a header to a body, or returns the
body untouched when there is none. `agent_card_header(mailbox_id, agent_id)`
answers the same question for a turn card by resolving where that card actually
lives, which matters because a turn dispatched from the root Control chat keeps
its card there.

So inside a topic the messages are now `📨 Queued`, `🎙️ Transcribing…`,
`🧠 Codex is working…`, `⏹ Cancelled.`, and bare answers. In the root Control
chat — relayed answers, dispatch previews, replies continued from there, and the
fallback resend — the project name is retained, because there it is the only
thing identifying who is speaking. `labeled_agent_chunks` takes optional
delivery coordinates for exactly this reason: the same response is labeled when
relayed and unlabeled in its own topic. `agent_voice_status_text` treats an empty
speaker as "no speaker line" rather than defaulting to `Agent`.

Verified by updating every affected assertion in both directions — the in-topic
receipts, transcribing notices, working cards, voice status, scoped skill
updates, and final answers lost their labels, while the two root-chat cases kept
theirs, which is what proves the discriminator works rather than a blanket
removal. Full suite green at 350 tests.

## Pinning removed, command menu kept

Once the command menu was registered, pinning had no job left: typing `/` shows
every command in any chat or topic, which is what the pin was reaching for. So it
was taken out rather than maintained — `pinChatMessage`,
`unpinAllForumTopicMessages`, the `pin_messages` right in the `/newgroup` link,
the sender's pin-specific failure classification and its `outbox_pin_skipped`
event, and the test that covered a rights failure.

The `topic_intro` card survives with mode `record`: it exists only so the outbox
completion transaction can store the message ID Telegram assigned, which is what
lets later turns edit the topic's opening message in place. Its integration test
now asserts the opposite of what it once did — that no pin or unpin call is
queued, and that the acknowledged ID lands on the subject.

The lesson worth keeping is in `CLAUDE.md`: `telegram_help.COMMANDS` is the one
source of truth for the menu and the `/help` page, and adding a command means
updating that tuple, handling it, and running `sync-commands` — because the menu
is published, not derived, so an unregistered command is effectively hidden.

Full suite green at 349 tests.

### Restarts are queued, not timed

Applying worker-side code meant a choice between aborting live turns and
babysitting a retry loop: `restart-if-idle` refuses while anything is leased, and
the turn asking for the restart is itself a leased row, so it can never succeed
from inside that turn. On July 26, 2026 the only way through was a detached tmux
watcher polling every fifteen seconds; it worked — the restart landed on attempt
four, once the asking turn had replied — but nothing about it belonged in a
durable system.

`request-restart` writes the intention into `controller_state` instead. The
supervisor checks every five seconds and applies it by exiting, which launchd's
`KeepAlive` turns into a full reload onto current code. `claim_idle_restart`
takes and clears the request in one transaction, and only while
`leased_work_counts()` is empty across the inbox, router, agent, and outbox
queues — so a turn beginning a moment later is never caught by a restart that had
already decided the system was quiet, and a relaunched supervisor cannot loop on
a request it already consumed. Claims are recorded as
`controller_restart_claimed` events, and `status` reports a pending request
alongside whatever is currently blocking it.

The asking turn no longer has to wait, watch, or hand the job to tmux: it queues
and finishes, and its own completion is what unblocks the restart.

Verified with a test that queues a restart while a turn holds a lease, asserts it
cannot be claimed, drains the turn and its outbox, then asserts a single claim
succeeds, clears the request, records one event, and cannot be claimed twice.
Full suite green at 351 tests.

### Menu taps in a group carry the bot's username

Registering the menu exposed a second bug immediately: in a group, tapping a
command inserts `/agent@yourbot`, because a group can hold several bots and
Telegram disambiguates. Every command comparison in the handler was against the
bare string, so a tapped command missed all of them, fell through to
`route_user_input`, and the agent answered a message that was meant for the
controller. In the private bot chat there is no suffix, which is why this only
showed up once the menu existed.

`addressed_command(text)` now strips a trailing `@<bot_username>` from the first
token — case-insensitively, preserving arguments, so `/agent@bot create foo`
becomes `/agent create foo` — and the whole dispatch chain matches on its result.
A command addressed to a *different* bot is returned untouched, so it is not
claimed as ours. The bot's own username comes from `TELEGRAM_BOT_USERNAME` in the
handler environment, with `config.json` as the fallback.

Verified with an integration test that sends `/projects@example_bot` into a group
topic and asserts the catalog reply with no router turn, then sends
`/projects@someone_else` and asserts it is routed as ordinary text instead.
Because `on_message.py` is re-read per turn, this fix was live without a restart.

Handling it correctly still left the suffix on screen, which is Telegram's client
writing into the input field — no Bot API call influences that, and
`setMyCommands` has no flag for it. The one real lever is BotFather's Group
Privacy setting: `getMe` reports `can_read_all_group_messages: false`, meaning
privacy mode is on, so a bare `/status` would not be delivered to this bot in a
group and the client addresses it explicitly to compensate. Turning Group Privacy
off is the owner's call in BotFather; nothing here changes behavior when it does,
because promoting the bot to administrator already gives it the same visibility
and the transport still discards every message that is not from the paired owner.

Independently of that setting, `remove_addressed_command_message` deletes the
command message once the command has been handled, but only when the text
actually carried the mention — a command the owner typed reads cleanly and is
left in place. The reply is what conveys the outcome, so the disposable half is
the request. Its test asserts the tapped form queues exactly one `deleteMessage`
for that message and the typed form queues none. Full suite green at 351 tests.

## Stage 0 legacy bridge commands

The original non-durable bridge remains in `telegram_bridge.py` and is still
useful for isolating the transport from the controller. Do not run it at the
same time as the durable collector: Telegram permits only one long poller per
bot.

The commands in this section are historical diagnostics and rollback tools,
not the supported live-update procedure. Current installations should use
`telegram_control.py bootstrap` for first installation and the queued
`request-restart` workflow for worker reloads.

Computer to phone:

```sh
./telegram_bridge.py send "Hello from my Mac"
```

Phone to computer, foreground:

```sh
./telegram_bridge.py listen
```

Send the bot either text or a Telegram voice note. Text is acknowledged by the
Mac-side handler. Voice is downloaded into a private temporary directory,
converted to 16 kHz WAV, transcribed locally, returned as text, and deleted.

Install or update the Stage 0 per-user LaunchAgent, inspect it, or remove only
the listener while retaining pairing and the Keychain token:

```sh
./telegram_bridge.py install --handler ./on_message.py
./telegram_bridge.py status
./telegram_bridge.py uninstall
```

The legacy `telegram-bridge` names remain in the runtime paths so the working
pairing and Keychain entry continue to work. A later migration will rename
runtime components only if it can do so without losing queued messages.

Prerequisites, first-time setup, runtime state locations, the test command, and
the security boundary are documented in [../README.md](../README.md).

## Direct mode is the default

The conversational Control agent is no longer part of the required request
path. Built-in settings disable it by default, and the supervisor omits
`work-router` unless `telegram_control.control_agent.enabled` is explicitly
true. The router schema, worker, and confirmation machinery remain available
for installations that still want natural-language discovery and delegation.

The paired private bot chat is now a deterministic setup and administration
home in direct mode. It exposes commands but does not turn arbitrary text,
voice, or attachments into Codex router work. An authorized group that still
needs a workspace accepts `/bind <exact-path>` or an exact path as its next
message. The handler resolves symlinks, enforces configured discovery roots,
validates the workspace, detects installed Claude and Codex CLIs, and presents
only locally available providers in a one-time confirmation card. A descriptive
folder answer continues to use the old discovery router only when Control is
opted in.

Once a group is bound, its provider choice is sufficient authorization to
create topic agents. New Telegram topics now inherit the group default
automatically; their first ordinary message creates the durable subject and is
queued directly as the first agent turn. The former per-topic confirmation card
is available through `telegram_control.topics.confirm_agent`, while `/agent`
continues to expose provider, model, effort, permission, and session changes at
any time.

Provider discovery is shared by setup and adapters, so Claude and Codex are
reported independently instead of constructing the wrong adapter to discover a
missing binary. Non-secret Telegram Control settings are passed from the
worker's already-loaded config into each fresh handler process, avoiding
per-turn configuration drift.

Verified with direct-mode tests covering the private admin home, exact-path
binding with Claude and no router mailbox, provider capability discovery,
automatic first-message startup, opt-in topic confirmation, and the complete
legacy Control path when explicitly enabled. The 174 durable-store and
integration tests pass after both modes were separated.

## Readiness is capability-based

`doctor` now defines one core: local macOS, Python 3.9 or newer, a valid paired
handler and Keychain token, a healthy SQLite store, and at least one runnable
Claude Code or Codex CLI. It reports the two providers separately and rejects a
configured default that is not installed. Enabling the optional conversational
Control agent adds Codex to the core requirements because that legacy router is
still a Codex agent.

Handy, its Parakeet model, ffmpeg, edge-tts, and tmux are reported as optional
capabilities. Missing voice input, spoken-reply, console, or detached-worker
support no longer makes a text-only installation look broken. The report also
states the authentication boundary precisely: it verifies that a provider CLI
runs, while the provider's own terminal login remains the source of truth.

After Telegram pairing, `telegram_control.py bootstrap` runs the readiness
check, installs the durable controller and shared skills, publishes the command
menu, and prints live status. It is the single installation entry point for a
person or coding agent; the lower-level `init`, `doctor`, `install`, and
`install-skills` commands remain independently useful.

Verified with readiness tests for a Claude-only text installation, the
Control-without-Codex rejection, executable version reporting, and bootstrap
ordering.

## Customization is layered, scoped, and subordinate to safety

Behavior settings now resolve in a fixed order: built-in defaults, the
per-install `telegram_control` object in private `config.json`, shared
`.telegram-control.json` workspace settings, then the ignored
`.telegram-control.local.json` personal workspace override. Unknown keys,
invalid enum values, conflicting inline/file prompts, oversized prompts, and
relative prompt files that escape their workspace all fail with a specific
configuration error. `telegram_control.py config show --effective
--workspace <path>` prints the merged result and every participating path.

The customizable prompt surface has two intentionally separate fields:
`preamble` supplies standing user or project context and `response_style`
describes how the agent should communicate. Each can instead name a Markdown
file. These sections are appended after the non-replaceable Telegram Control
turn contract; they cannot delete or replace its lifecycle, capability, or
background-work safety instructions. Both the Claude system-prompt adapter and
Codex developer-instruction adapter receive the same effective guidance.

`presentation.status_style` controls the durable topic header. `compact` keeps
only the topic, provider/model/effort, context, and immediate state;
`standard` preserves the normal command-oriented header; `detailed` adds the
workspace, permission mode, provider-session state, and lifecycle. Rendering
reads the same install and workspace layers, so a personal local override
changes one person's installation without changing repository defaults.

Verified with configuration precedence and validation tests, contained
Markdown prompt-file tests, byte-for-byte preservation of the default core
guidance, custom-guidance ordering, compact topic rendering, and effective
configuration inspection.

## Documentation follows the supported user journey

The root README is now an orientation and quick-start page rather than the only
manual. `docs/README.md` indexes focused local-Mac installation, Telegram group
setup, provider/capability, customization, configuration, security, and
architecture documents. The supported path is explicit: pair the bot, run
`bootstrap`, bind a private topic-enabled group to an exact local folder, and
start an agent conversation by sending the first message in a topic.

The requirements table distinguishes the one-provider core from optional local
voice input, external spoken replies, and tmux features. It states that a
Claude-only direct-mode installation does not require Codex, while the opt-in
conversational Control agent does. Linux, EC2, containers, and multi-host
operation are recorded as deferred rather than implied variants of the
LaunchAgent/Keychain design.

Telegram group documentation distinguishes enabling Topics from the
per-account **View as Topics** versus **View as Messages** display choice and
strongly recommends topic view. Screenshot slots are documented with a
sanitization checklist; no image from the maintainer's live bot, groups, local
paths, or provider sessions was committed.

The in-Telegram Projects & topics help page now describes the same direct
workflow, `/bind` requirement, automatic topic startup, optional Control
agent, and Claude-only behavior. The registered command list remains the
single source of truth and all 174 durable-store/integration tests pass with
the updated help copy.

## The repository carries an explicit collaboration contract

The project now includes Apache-2.0 licensing and notice files, contribution
and governance policies, a security reporting path, a code of conduct,
CODEOWNERS, structured bug and feature forms, and a pull-request checklist.
Governance gives the lead maintainer final responsibility for the canonical
project while making a clear distinction between personal configuration,
ordinary pull requests, and deliberately divergent forks.

The macOS CI workflow compiles the tree and runs the complete dependency-free
test suite on the oldest supported Python line (3.9) and a current line (3.13).
Both third-party actions are pinned to full commit SHAs, the workflow token is
read-only, concurrent superseded runs are cancelled, and each job has a
bounded timeout.

The first hosted run exposed three tests that accidentally depended on the
maintainer Mac's paired config or installed Codex binary. The voice-handler
tests now supply the same serialized settings environment as the real worker,
and the tmux collision test injects its provider adapter. The workflow uses the
current Node 24/ESM GitHub-owned actions with checkout credentials explicitly
disabled after checkout, so the dependency-free claim is exercised on a clean
runner rather than masked by the live installation.

Repository files cannot enforce GitHub permissions. The maintainer guide
therefore records the owner-only work: confirm the proposed license; audit the
complete history before changing visibility; create an active `main` ruleset
requiring pull requests, resolved conversations, both CI checks, and protected
history; keep required approvals at zero while there is only one reviewer;
raise it to one with CODEOWNER review when a second maintainer exists; restrict
Actions and collaborator access; and enable the available security features.

## Workspace inventory spans projects and bound groups

The original `/projects` implementation read only the `managed_projects`
catalog. Private group setup later introduced `forum_workspaces` and
`forum_subjects`, so a valid group bound directly to a folder could own many
persisted topic sessions without appearing in `/projects` or in the optional
Control agent's workspace context.

`DurableStore.list_workspace_inventory()` now builds a path-free read model
across both storage systems. It deduplicates an enrolled project and a bound
forum only when their validated workspace-root and working-directory pair both
match, aggregates active topic and provider-session counts across every forum
using that workspace, and never exposes either stored path. It does not copy
forum rows into `managed_projects`, assign unstable synthetic slugs, or require
a schema migration.

The direct `/projects` command and Control's `list_projects` response render the
same connected-workspace inventory. Control's prompt also receives this
inventory separately from the enrolled-project tool catalog: forum-only
workspaces are visible with a null project slug, while slug-gated project tools
remain limited to actual `managed_projects` rows. This preserves validation and
avoids implying that Control can dispatch to an arbitrary topic in another
group.

Verified with store coverage for project/forum deduplication and active session
aggregation, path-safe rendering coverage, router-prompt coverage for
forum-only workspaces, and an end-to-end `/projects@bot` group-command fixture
whose only workspace is a bound forum.

The catalog is rendered as escaped Telegram HTML rather than MarkdownV2.
Workspace names are bold, providers and durable slugs use code styling, a
single summary line reports total workspaces/topics/sessions, and blank space
separates entries. Every database-derived label is HTML-escaped before it
reaches Telegram. Both the direct command and Control router set
`parse_mode=HTML`; router fallback sends infer that formatting from the durable
`list_projects` tool name, so a late receipt or failed edit cannot expose raw
tags.

## Telegram messages share one safe formatting boundary

`telegram_formatting.py` now centralizes the Control speaker label, controller
HTML escaping and balanced chunking, semantic status icons, and provider
Markdown rendering. Help pages and the workspace catalog use the shared
controller boundary: authored structure may use Telegram HTML, while every
dynamic value is escaped. Long HTML messages close and reopen active tags at
chunk boundaries rather than risking a split tag or entity. Literal controller
messages keep their bodies unchanged and use an explicit entity only to bold
the trusted speaker label.

Claude and Codex output is compiled to explicit Telegram `MessageEntity`
objects, never passed through a Telegram parse mode. The documented subset
supports headings, emphasis, inline and fenced code, safe links, lists, task
markers, quotes, and dividers. Unsupported inline syntax stays literal; an
invalid fenced document falls back to its exact source. Entity offsets use
UTF-16 code units and are recomputed after speaker labeling and chunking, so
emoji and spans crossing message boundaries remain valid. Provider source text
continues to be stored unchanged for retries, session history, and TTS.

The durable sender also has a delivery fallback: if Telegram rejects a message
entity payload, it atomically removes the entities and immediately retries the
same visible text. This needs no schema migration and is compatible with older
workers because outbox parameters were already forwarded generically.

Verified with focused renderer tests for Markdown constructs, ambiguous
underscores, malformed fences, non-BMP UTF-16 offsets, cross-chunk spans,
escaped controller content, and balanced HTML. Durable integration coverage
asserts formatted provider final edits, immediate entity-rejection fallback,
formatted `/help` sends and edits, and unchanged multi-chunk delivery
semantics.

The direct `/agent` status command now follows the same controller hierarchy
instead of emitting one dense plain-text field list. Its escaped HTML card has
separate **Runtime**, **Workspace**, and conditional **Usage** sections;
provider, model, effort, workspace, and working-directory identifiers use code
styling, while labels and section headings provide the scan hierarchy. The
existing lifecycle, session, model, effort, and provider buttons are unchanged.
Because `/agent` is handled by a fresh `on_message.py` process, this presentation
change takes effect on the next command without waiting for the long-running
workers to reload.

## Confirmed group teardown removes the workspace and every topic

`/removegroup` is the group-level counterpart to `/newgroup`. The owner can
send it in any ordinary topic of a bound private forum. The handler creates a
30-minute, owner/chat/topic-bound confirmation card directly, without starting
a Claude or Codex turn. Its summary includes the active managed-topic and
detached-worker counts and explains that Telegram Control cannot delete the
Telegram group itself.

Confirmation re-resolves the exact active root binding inside an immediate
transaction. A queued or leased topic turn, active console, Control router
turn, or intended-running detached worker blocks removal and leaves the
repeatable confirmation available. Stopped workers have only their exact
controller-owned recovery file removed; unexpected companion files are
preserved.

Successful teardown atomically expires every callback in the group, revokes
reply routes, stales surface cards, archives forum subjects and their managed
agents, clears provider-session pointers, removes stopped worker records,
revokes every topic and root surface, and marks the forum workspace revoked.
One idempotent `deleteForumTopic` call is queued for every managed and
report-only topic under a shared serialization key. Already-missing topics use
the sender's existing idempotent success path. A final unthreaded message lands
in General and tells the owner that the bot or Telegram group can now be
removed. Historical inbox, outbox, agent, subject, and event rows remain as an
audit trail.

Verified with durable-store coverage for active-worker blocking, complete
multi-topic/agent/worker archival, serialized Telegram deletions, and callback
expiry; handler integration coverage exercises the direct command and its
confirmed end-to-end teardown without creating an agent or router turn.

## Spoken replies have a preview-first configuration flow

`/voice` opens an owner/chat/topic-bound inline picker for the installation's
global spoken-reply voice and speed. The curated choices cover British,
American, Australian, and Indian English voices plus four speaking rates.
Selecting either value stages a complete configuration and edits the same card
into a review screen; it does not mutate live settings. **Preview** is reusable
for the life of the card and generates a real Microsoft TTS OGG/Opus sample,
**Confirm** persists the staged values, and **Back** returns to fresh choices
without applying anything. Opening a new picker or navigating between screens
expires older voice-configuration callbacks on that exact surface.

The confirmed configuration is stored as validated JSON in the existing
`controller_state` table, so no schema migration or worker restart is needed.
It is read at synthesis time by completed-answer Listen actions, scoped agent
voice updates, and detached-worker voice reports. The built-in fallback
remains Sonia at +10%; invalid stored or callback values fail closed rather
than reaching `edge-tts`.

Verified with focused tests for configuration validation and persistence,
voice/rate command arguments, command registration, and the complete
picker → staged review → preview → back → confirm flow. Preview coverage also
asserts that the stored setting remains unchanged until confirmation.

## Documentation audit against current direct-mode behavior

After merged pull requests #1–#4, the public documentation and `/help` copy
were checked against the current command dispatcher, configuration validator,
provider/session controls, detached-worker recovery path, teardown planners,
CLI rollout commands, and schema v23 store.

The audit corrected several historical statements that had outlived their
implementation: provider choice happens at `/bind`, `doctor` checks CLI
executability rather than authentication, command-menu publication is
best-effort and retriable, detached workers no longer maintain a recovery
inventory, busy agent reconfiguration is rejected rather than deferred, and
the repository is already public under Apache 2.0. It also documents all nine
registered commands, the current module map, machine-local configuration keys,
the older `/agent create <slug>` catalog path, uninstall retention semantics,
and the requirement to wait for durable topic deletions before removing the bot
from a group.

The `/newgroup` card now includes Telegram's separate **View as Topics** choice
and gives the current direct-mode next step: authorize the forum, then send
`/bind` with an exact existing folder. The private `/start` response likewise
points to `/newgroup` and `/projects` instead of implying that ordinary private
text will reach a disabled Control router. These are fresh-handler copy changes;
they need no schema migration, command-menu republish, skill installation, or
worker restart.

Verified on July 27, 2026 with all 398 unit and integration tests,
`python -m compileall`, a local Markdown-link resolution check, and
`git diff --check`.

## Claude steering survives long foreground tool calls

The July 27 Reservations release-watch setup exposed a second live-control
failure after worker-exit recovery was deployed. The managed parent Claude turn
started a foreground `until tmux capture-pane ...` loop waiting for text that
the detached worker never printed. Claude and its controller worker remained
healthy, and the detached worker reported successfully, but the Claude SDK did
not echo a later steering message while the Bash tool call was still active.
The adapter treated the missing echo after 30 seconds as a rejection even
though the message had already been written to Claude's input stream and could
still be processed after the tool returned.

Claude steering is now tracked by message UUID for the lifetime of the provider
turn. It no longer expires on the interrupt timeout: each steer remains
`delivery_in_flight` until Claude echoes that exact UUID or the turn finishes.
Multiple pending steers do not prevent Control from claiming a later Stop;
interrupts retain their 30-second process-group fallback. This keeps the
Telegram receipt truthful during a long tool call and preserves independent
Stop delivery.

The detached-worker skill also forbids parent-turn polling. After
`worker-start` and `worker-brief` succeed, the parent may take one immediate
pane snapshot but must finish instead of waiting for a milestone. Durable
`worker-report` messages provide progress, and later corrections are relayed
with another file-backed `worker-brief`.

For the live incident, the release-watch correction was delivered directly to
the healthy detached worker. Only the parent turn's polling shell process group
was terminated; the Claude provider, detached worker, and its scheduled work
were left running.

Verified with two new Claude adapter regressions covering delayed steering
acknowledgment and Stop delivery behind an unacknowledged steer, the complete
405-test suite, Python compilation, and `git diff --check`.

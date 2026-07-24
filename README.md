# Telegram Control

Telegram Control is a phone-to-Mac control plane for local Codex and Claude
agents. The intended experience is:

1. Send text or a voice note in Telegram.
2. Transcribe voice locally with Handy's Parakeet V3 model.
3. Route the request to the main Codex controller or a project agent.
4. Receive text, a Telegram voice note, and contextual buttons that route
   follow-up actions back to the originating agent.

The repository currently contains the working Stage 0 proof of concept plus the
plan for building the durable controller. See [docs/BUILD_PLAN.md](docs/BUILD_PLAN.md).

## What works now

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
/Users/shantam/telegram-control/telegram_control.py init
/Users/shantam/telegram-control/telegram_control.py status
/Users/shantam/telegram-control/telegram_control.py doctor
```

Run the durable transport in the foreground:

```sh
/Users/shantam/telegram-control/telegram_control.py run
```

After the live foreground smoke test passes, migrate the existing LaunchAgent
in place:

```sh
/Users/shantam/telegram-control/telegram_control.py install
```

The durable installer retains the existing LaunchAgent label so the Stage 0
poller is unloaded before Stage 1 starts. Do not run Stage 0 `listen` alongside
the Stage 1 collector because Telegram permits only one long poller for a bot.
Restore Stage 0 at any time with:

```sh
/Users/shantam/telegram-control/telegram_bridge.py install \
  --handler /Users/shantam/telegram-control/on_message.py
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
/Users/shantam/telegram-control/telegram_control.py topic-capability
```

Create and durably bind a managed project topic:

```sh
/Users/shantam/telegram-control/telegram_control.py \
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
/Users/shantam/telegram-control/telegram_control.py \
  register-agent "Project Topic" project-slug /absolute/project/path
```

Registration atomically creates the project agent and changes the topic binding
from `controller/control` to `agent/<agent_id>`. Repeating the same registration
returns the existing agent; mismatched registrations and invalid slugs fail
closed. Send `/agent` inside the topic for a path-safe registry, session,
console, and latest provider-usage summary.

The live registry test passed on July 23, 2026. **Stage 2 Test** is bound to
`tc--root--telegram-control` using the Codex provider and reports `registered`
with no provider session started.

Schema version 7 adds a serialized durable mailbox per managed agent and a
provider-neutral execution contract. The first adapter uses structured JSONL
from `codex exec --json`, checkpoints `thread.started` before turn completion,
parses the final public agent message and usage metadata, and resumes the stored
session ID. Codex runs with `workspace-write` by default; unrestricted/yolo
mode is never enabled by the controller.

The Stage 5 provider expansion adds Claude Code behind that same contract.
Claude runs in non-interactive stream-JSON mode, checkpoints its session UUID
before completion, normalizes the final result and usage, and resumes the same
conversation on later mailbox turns. Managed Claude agents default to
`bypassPermissions` because unattended print-mode turns cannot answer
permission prompts; set `provider_config.permission_mode` to `acceptEdits`,
`auto`, `dontAsk`, or `plan` for a more restrictive agent. The explicit tmux
console can resume either a Codex or Claude session without changing its
logical agent identity.

Each accepted agent turn immediately sends a compact `⏳ Working…` receipt.
For normal
single-message responses, completion edits that same Telegram message into the
final answer. The receipt and completion paths are race-safe: if the provider
finishes first, the final edit is created when Telegram returns the receipt's
message ID. If Telegram later rejects the edit, the answer falls back to a new
routed message instead of being lost. Responses requiring multiple Telegram
chunks retain the receipt and use separate final messages.

The live adapter test passed on July 23, 2026. Two read-only Telegram turns
completed through the serialized mailbox on their first attempts. The
controller was restarted between turns, both used the same persisted Codex
session ID, and the second turn produced an immediate receipt followed by the
final response.

Schema version 8 adds an optional interactive tmux console. Structured adapter
turns remain the normal control path; the console is an explicit takeover of an
already-persisted session:

```sh
/Users/shantam/telegram-control/telegram_control.py \
  console-open tc--root--telegram-control
/Users/shantam/telegram-control/telegram_control.py \
  console-status tc--root--telegram-control
tmux attach-session -t '=tc--root--telegram-control'
/Users/shantam/telegram-control/telegram_control.py \
  console-close tc--root--telegram-control
```

Opening fails unless the agent has a persisted provider session and an idle
mailbox. The reservation prevents the mailbox worker from controlling the same
agent concurrently. New Telegram turns may still queue durably during takeover
and are claimed after the console closes. Existing unmanaged tmux sessions are
never adopted or killed, and Codex retains the configured sandbox rather than
enabling unrestricted/yolo mode.

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

The live console test passed on July 23, 2026. The real Codex TUI resumed the
same conversation in tmux, `/agent` reported `Console: running`, and a Telegram
turn remained queued until `console-close`. It then completed on its first
mailbox attempt with the expected response.

The live self-editing turn test passed on July 23, 2026. The durable receipt was
delivered first and then replaced in place by the exact Codex response, with no
second final message. One observed slow receipt was traced to an isolated
8.9-second delay before the Telegram update reached the controller; the
controller queued the receipt within 0.46 seconds of ingestion.

Voice notes sent inside a managed agent topic use that same turn card. The
`🎙️ Transcribing…` receipt is queued before local download, ffmpeg conversion,
and Parakeet V3 transcription. The same message then shows `📤 Sending:` with
the transcript, changes to `🧠 Codex is working…` or
`🧠 Claude is working…`, and finally becomes the agent's response. Durable
outbox ordering prevents a delayed progress edit from
overwriting the final answer. Voice notes outside a managed agent surface
retain the original transcript-only behavior.

The live managed-voice test passed on July 23, 2026: one voice note progressed
through every status stage and returned the requested exact Codex response in
the same Telegram message.

Controller-owned progress text uses Telegram HTML formatting with escaped
dynamic content: stage labels are bold and voice transcripts are block quotes.
Provider output remains plain text until a tested renderer can safely support a
documented Markdown subset without interpreting arbitrary model text as markup.

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
/Users/shantam/telegram-control/telegram_control.py \
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
/Users/shantam/telegram-control/telegram_control.py collect
/Users/shantam/telegram-control/telegram_control.py work
/Users/shantam/telegram-control/telegram_control.py work-router
/Users/shantam/telegram-control/telegram_control.py work-agents
/Users/shantam/telegram-control/telegram_control.py send-outbox
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
  Control chat, the response edits that receipt in place, and the new final
  message routes back to the same agent, so follow-up replies chain
  indefinitely.
- The reply route is revalidated durably at enqueue time: a reply from the
  wrong chat or topic, to the wrong message, after route expiry, or against a
  different agent fails closed.
- If Telegram permanently rejects the final edit, the existing fallback sends
  the response as a new agent-routed message. The acknowledged dispatch card
  and fallback both continue to reach the same agent, so nothing is lost.
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
- When a receipt was already delivered, a multi-chunk response edits the first
  chunk into the receipt and sends the remaining chunks as follow-up messages,
  so no `⏳ Working…` receipt is left behind.
- Voice replies follow the same durable routes as text replies. A voice note
  recorded as a reply to a retargeted agent answer continues that exact agent
  and persisted provider session: the reply surface gets the usual
  `🎙️ Transcribing…` receipt, the local transcript becomes the agent input,
  and the one Telegram message progresses to the agent's final response. The
  stored route is revalidated durably before any mailbox work is created, so
  foreign, stale, or mismatched voice replies fail closed exactly like text.

## Live Codex worker control

Schema v17 persists the provider turn ID separately from the provider session
ID and adds a durable control queue for active worker turns:

- A turn card progresses from `📨 Queued` through bounded Codex-authored
  statuses and exposes a one-time `⏹ Stop` button while work is active.
- Replying to the exact active turn card queues guidance for that exact Codex
  turn through app-server `turn/steer`. Replying to any other agent message
  starts an ordinary follow-up turn instead.
- Stop is durable before and after the provider turn ID becomes available. It
  is delivered through app-server `turn/interrupt`; a cancellation is terminal
  and is never retried as a failed turn.
- Controls are tied to the active mailbox lease and expected provider turn.
  Lease recovery rejects uncertain in-flight controls instead of replaying
  them against a replacement turn.
- Status, terminal, voice, and router-card edits share per-turn serialization
  keys. Terminal completion supersedes queued status edits and removes the
  inline keyboard, preventing an old `Working` or `Stop` state from
  overwriting a final response.

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
A first text or voice message in a new private forum produces one
owner/topic-bound **Authorize forum** button and does not reach Control. After
that explicit confirmation, resend the request: its topic becomes a
Control-bound surface under the authorized forum using the existing
`(chat_id, message_thread_id)` key. Consequential workspace or agent creation
remains separately confirmation-gated. Multiple bot tokens are not required.

An authorized forum can then be bound conversationally to one existing local
workspace, including a non-Git notes tree. Control resolves the user-stated
path or an enrolled project, validates the workspace and optional working
directory, and presents a one-time **Bind forum workspace** confirmation. The
durable forum record stores the realpath-resolved boundary, optional exact Git
root, and Codex model/effort defaults. Repeating the same confirmation is
idempotent; silently rebinding an active forum to another directory is
rejected.

After a forum is bound, the first ordinary text or voice message in each topic
atomically creates one durable subject, one topic route, and one managed Codex
worker. Later messages reuse that subject and its persisted provider session;
topic renames update the user-facing subject label without changing its
identity. The worker inherits the forum's exact workspace boundary, working
directory, optional Git metadata, and model/effort defaults. Existing managed
agent mailboxes provide receipts, editable progress, reply-to-steer, Stop,
pause/resume, and explicit new-session controls—no new daemon or actor runtime
is introduced. Read-only commands such as `/status` do not create a subject,
and exact replies to earlier Control messages keep their durable Control route
for both text and voice even after the topic becomes a subject.

Live setup:

1. Create a private Telegram group and enable Topics.
2. Add Slam Paws and promote it to administrator so Group Privacy does not
   suppress ordinary text and voice.
3. Send any harmless text or voice request in a topic.
4. Tap **Authorize forum**, then resend the request.
5. Ask Control to bind the forum to an existing absolute workspace path and
   tap **Bind forum workspace**.
6. Send an ordinary request in any topic. Its first request creates the
   subject; later requests continue the same Codex session. Send `/status` in
   that topic to inspect or control its managed agent.

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

## Prerequisites

- macOS
- Telegram and a bot created with [@BotFather](https://t.me/BotFather)
- Handy with the Parakeet V3 model installed
- `ffmpeg` at `/opt/homebrew/bin/ffmpeg`

The current local transcription helper is:

```text
/Applications/Handy.app/Contents/MacOS/handy
```

The existing model files are expected under:

```text
~/Library/Application Support/com.pais.handy/models/parakeet-tdt-0.6b-v3-int8
```

## Set up or re-pair

Double-click `SETUP.command`, or run:

```sh
/Users/shantam/telegram-control/SETUP.command
```

The setup flow stores the Telegram bot token in Keychain under the service
`telegram-bridge-bot-token`. The token is never stored in this repository.

## Test the proof of concept

Computer to phone:

```sh
/Users/shantam/telegram-control/telegram_bridge.py send "Hello from my Mac"
```

Phone to computer, foreground:

```sh
/Users/shantam/telegram-control/telegram_bridge.py listen
```

Send the bot either text or a Telegram voice note. Text is acknowledged by the
Mac-side handler. Voice is downloaded into a private temporary directory,
converted to 16 kHz WAV, transcribed locally, returned as text, and deleted.

## Background listener

Install or update the per-user LaunchAgent:

```sh
/Users/shantam/telegram-control/telegram_bridge.py install \
  --handler /Users/shantam/telegram-control/on_message.py
```

Inspect it:

```sh
/Users/shantam/telegram-control/telegram_bridge.py status
```

Logs:

- `~/Library/Logs/telegram-bridge.log`
- `~/Library/Logs/telegram-bridge.error.log`

Remove only the listener, retaining pairing and the Keychain token:

```sh
/Users/shantam/telegram-control/telegram_bridge.py uninstall
```

## Current runtime state

Runtime state is deliberately outside Git:

- Token: macOS Keychain
- Pairing and handler configuration:
  `~/Library/Application Support/telegram-bridge/config.json`
- Telegram update offset:
  `~/Library/Application Support/telegram-bridge/offset`
- LaunchAgent:
  `~/Library/LaunchAgents/local.telegram-bridge.plist`

The legacy `telegram-bridge` names remain during Stage 0 so the working pairing
and Keychain entry continue to work. A later migration will rename runtime
components only if it can do so without losing queued messages.

## Tests

The Stage 0 tests are dependency-free:

```sh
/usr/bin/python3 -m unittest discover -s tests -v
```

They exercise token input sanitization and basic message metadata without
calling Telegram or reading the Keychain. Stage 0 uses the same Apple/Xcode
Python 3.9 interpreter as the LaunchAgent.

## Security boundary

Incoming Telegram content is data, never a shell command. The current handler
path is fixed in local configuration. Future buttons will contain opaque,
short-lived action IDs—not shell fragments, prompts, filesystem paths, or
privileged data. Every inbound event will be checked against the authorized
Telegram user/chat and its durable route record before an action is taken.

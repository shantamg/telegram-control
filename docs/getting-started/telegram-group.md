# Set up a Telegram project group

Telegram Control works best as one private group per project and one topic per
agent conversation.

## Create and authorize the group

1. Send `/newgroup` in the bot's private chat.
2. Create a new **private** Telegram group.
3. Open the group's settings and enable **Topics**.
4. Return to the bot's private chat and tap the add-to-group link.
5. Choose the new group and approve the requested admin rights.
6. The bot automatically posts a welcome card in General. Follow its setup
   steps and confirm the requested changes.

Telegram's Bot API cannot create a group or enable Topics, so those remain
human steps. The bot requests Change group info, Delete messages, and Manage
topics. Admin access also ensures Telegram's default Group Privacy setting does
not hide ordinary messages from the bot.

The welcome card explains that General is for setup and administration, shows
the authorization and workspace-binding sequence, and tells the owner where
the first agent conversation will appear. It is generated from the
add-to-group `/start` arrival; no extra message or command is required.

Public groups and groups with a public username are rejected by design.

### The install-time Telegram Control group

Installation seeds the resolved repository checkout in the durable database
under the name **Telegram Control**. When the first unclaimed private forum
with that exact name adds the bot, its setup card offers the seeded checkout
and available provider choices immediately. Confirming the card both authorizes
the group and binds it; no path needs to be typed. Confirmation also applies
the bundled 🎛 group avatar. The seed is claimed by that chat ID, so another
group with the same title cannot silently reuse its checkout or default icon.

Telegram's Bot API still cannot create the group or enable Topics. The seed
removes the local-folder step after those two human actions; it does not
hardcode a personal Telegram chat ID.

## Use “View as Topics”

Enabling Topics and choosing how to display them are separate settings.
Telegram offers **View as Topics** and **View as Messages**. Choose **View as
Topics** for a Telegram Control group.

In topic view, the group opens as a list of separate conversations, which
matches Telegram Control's routing model: one topic is one persisted agent
session. “View as Messages” blends messages from every topic into a conventional
group timeline and makes independent agent conversations much harder to follow.

This is an account/client display preference rather than a bot-controlled
group permission. Telegram documents that the choice is associated with the
account and synchronized to its other logged-in sessions. If the group starts
showing one combined timeline, open its menu and switch back to **View as
Topics**.

## Bind the local folder

In the authorized group, send an exact path:

```text
/bind ~/Software/my-project
```

The folder must already exist, be within the configured discovery roots, and
be safe to use as an agent workspace. Telegram Control resolves symlinks and
shows the exact path and available provider choices before changing anything.
Confirm the binding. Discovery defaults to your home directory; for a workspace
on another allowed volume, add an absolute path to `discovery_roots` as
described in the [configuration reference](../reference/configuration.md).

Descriptions such as “my project in Software” require the optional
conversational Control agent. Direct mode intentionally uses deterministic
paths and does not spend a provider turn guessing which folder you meant.

If setup is completed from Telegram's special **General** topic, Telegram
Control automatically creates a normal topic named **Start Here**. If `/bind`
was sent from an ordinary topic, that topic becomes the first conversation
instead and no duplicate is created.

## What General is for

General is usable for setup and group-level administration, including
authorization, `/bind`, `/help`, `/status`, `/projects`, and `/removegroup`.
Some Telegram updates from General omit `message_thread_id`; Telegram Control
normalizes those updates to Telegram's reserved General topic ID, 1.

General deliberately does not become an agent conversation. Telegram does not
allow deleting it with the ordinary `deleteForumTopic` method and exposes
separate lifecycle methods for it, which conflicts with Telegram Control's
normal one-topic/one-disposable-session teardown contract. Use **Start Here**
or another ordinary topic for agent work.

## Start talking

Open **Start Here**, reuse the ordinary topic where you ran `/bind`, or create a
new Telegram topic and send a message. In the default configuration:

- the topic is immediately provisioned with the group's provider;
- your message is queued as its first turn;
- the initial status message is edited in place as provider, model, effort,
  session, and context change.

No central Control topic and no extra “choose an agent” tap are required. Use
`/agent` inside a topic to inspect or change that conversation. Use `/help` to
browse the complete command guide.

An active agent can also create another ordinary conversational topic when you
ask it to. That new topic is independent and directly steerable. This differs
from a detached worker's report-only topic.

## Remove the project group

Wait for queued and active turns to finish, close active consoles, and stop the
group's detached workers. Then send:

```text
/removegroup
```

from General or any ordinary topic in the bound group. The confirmation card
reports how many managed topics and detached workers belong to the group.
Confirming permanently deletes every controller-managed Telegram topic and its
message history, archives the topic agents, clears provider sessions, removes
stopped worker records and recovery files, and revokes the workspace binding,
routes, buttons, and cards.

Telegram's Bot API cannot delete the group itself. The first tap acknowledgment
only says that cleanup is queued. Wait until the managed topics have actually
disappeared and the completion message arrives before removing the bot from the
group or deleting the group in Telegram.

## Suggested screenshot set

The first public documentation pass intentionally avoids screenshots that
might expose a Telegram account, private group, bot username, or local path.
The checklist in [`docs/assets/screenshots/README.md`](../assets/screenshots/README.md)
defines the sanitized screenshots to add later.

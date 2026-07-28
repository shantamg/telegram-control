"""Maintainable copy for Telegram Control's button-driven help browser."""

from __future__ import annotations

import re
from typing import NamedTuple

from telegram_formatting import escape_html


HELP_HINT = "Type /help to browse Telegram Control help."


class HelpTopic(NamedTuple):
    slug: str
    label: str
    text: str


class BotCommand(NamedTuple):
    command: str
    description: str


# Registered with Telegram so every command is tappable from the compose field's
# menu in any chat or topic, with no pin and no scrolling. This tuple is the one
# source of truth: the help home page below is rendered from it.
COMMANDS = (
    BotCommand("help", "Browse the Telegram Control guide"),
    BotCommand("agent", "Inspect or manage this topic's agent"),
    BotCommand("status", "Inspect this Telegram surface"),
    BotCommand("voice", "Choose and preview the spoken-reply voice"),
    BotCommand("projects", "List connected workspaces and topic sessions"),
    BotCommand("newgroup", "Get the link that adds me to a new project group"),
    BotCommand("bind", "Bind this group to an exact local folder"),
    BotCommand("removegroup", "Safely remove this project group and all topics"),
    BotCommand("teardown", "Safely remove this managed topic and session"),
)


HOME_TEXT = (
    "Telegram Control help\n\nChoose a topic below.\n\nQuick commands:\n"
    + "\n".join(
        f"/{command.command} — {command.description}" for command in COMMANDS
    )
)

_COMMAND_REFERENCE = re.compile(r"(?<![A-Za-z0-9_])(/[a-z][a-z0-9_]*)")


def _inline_html(text: str) -> str:
    escaped = escape_html(text)
    return _COMMAND_REFERENCE.sub(r"<code>\1</code>", escaped)


def _document_html(text: str) -> str:
    """Render authored help copy with a consistent Telegram HTML hierarchy."""

    lines = text.splitlines()
    rendered: list[str] = []
    for index, line in enumerate(lines):
        inline = _inline_html(line)
        if index == 0 and inline:
            rendered.append(f"<b>{inline}</b>")
        elif line == "Quick commands:":
            rendered.append("<b>Quick commands</b>")
        else:
            rendered.append(inline)
    return "\n".join(rendered)


HOME_HTML = _document_html(HOME_TEXT)


TOPICS = (
    HelpTopic(
        "agents",
        "Agents & sessions",
        """Agents and sessions

Use /agent inside a managed topic to inspect a grouped Runtime, Workspace, and Usage card covering its provider, model, effort, state, session, console, and context usage. In a managed topic, /status opens this same agent card; on a Control or setup surface, /status shows durable transport status with a Refresh button.

The /agent controls can change the current model and effort while preserving the existing conversation, pause or resume the agent, start a fresh session, resume a previous session, or switch providers. Codex and Claude each expose their own supported choices, and switching requires the destination CLI to be installed and authenticated. Conflicting changes are rejected while a turn or console is active; wait for it to finish, close the console, and try again. Session and provider changes also require the topic’s queue to be empty. Ordinary messages in the topic go to its bound agent.

Claude and Codex answers use safe native Telegram formatting for common Markdown such as headings, emphasis, links, lists, quotes, and code. If formatting cannot be validated or Telegram rejects it, Control falls back to plain text rather than losing the answer.""",
    ),
    HelpTopic(
        "detached",
        "Detached workers",
        """Detached workers

Ask the main topic agent to start long-running work in a detached session. The worker receives its own report-only topic in the same group and can post text or voice milestones there.

If tmux disappears, Telegram Control resumes the exact provider conversation. The resumed session retains its native scheduled work, so Control asks it to verify wakeups, loops, monitors, and background work, recreate anything missing, and explicitly report recovery success or failure. The worker is not asked to maintain a separate recovery inventory.

Messages in a report-only topic do not steer the worker. Return to the main agent topic to guide or stop it. Stopping removes the worker's managed recovery file; deleting its report topic remains a separate explicit choice.""",
    ),
    HelpTopic(
        "teardown",
        "Topic teardown",
        """Safe topic teardown

Type /teardown inside an active managed agent topic. Control opens the confirmation card directly without invoking Codex or Claude.

After confirmation, Telegram Control clears the provider-session pointer, archives the agent binding, revokes routes and buttons, and removes the Telegram topic. A queued or active turn must finish first. Control closes an active interactive console, but any detached worker created from this topic must be stopped. If confirmation is blocked, fix the named blocker and tap the same button again before the 30-minute card expires.""",
    ),
    HelpTopic(
        "removegroup",
        "Group removal",
        """Safe group removal

Type /removegroup in any ordinary topic inside the bound project group. Control shows how many managed topics and detached workers belong to the group, then asks for confirmation without invoking Codex or Claude.

Queued or active topic turns, active consoles, an active optional Control turn, and running detached workers block removal. Fix the named blocker and tap the same confirmation again before the 30-minute card expires. After confirmation, Telegram Control deletes every managed and worker topic, archives the topic agents, clears provider sessions, removes stopped worker records and recovery files, and revokes the group’s workspace binding, routes, buttons, and cards.

Telegram Control cannot delete the Telegram group itself. Once cleanup finishes, remove the bot from the group or delete the group in Telegram.""",
    ),
    HelpTopic(
        "voice",
        "Voice & replies",
        """Voice messages and replies

In a managed conversational topic, send a Telegram photo or any document normally, with an optional caption. Attachments up to 20 MB are saved privately and routed to Codex or Claude by local path.

Send a Telegram voice note there normally; it is transcribed locally and routed like text. If Telegram briefly fails to provide the audio file, Control retries behind the same transcribing receipt and shows an error only after every durable attempt fails. Report-only worker topics do not accept messages or commands.

Use /voice to choose the global Microsoft TTS voice and speaking speed used by Listen buttons, agent voice updates, and detached worker voice reports. Selecting an option only stages it. Preview generates a real sample voice note; Confirm applies it, and Back returns to the choices without changing anything.

Reply to an exact agent-routed message to continue that agent. A Control-routed reply continues the optional conversational Control agent only when it is enabled. During an active turn, a reply to its progress card can steer the work, and its Stop button interrupts the provider. Stop remains valid while the turn is active. If an owning worker exits, Control terminates that attempt’s provider process, restarts only the failed worker, and immediately retries or completes a queued Stop without disturbing other topics. Agents can also send requested voice or text progress updates through scoped Telegram skills.""",
    ),
    HelpTopic(
        "skills",
        "Telegram skills",
        """Telegram-specific skills

You can ask an active managed agent to:
• send a separate text progress update
• reply with a Telegram voice note
• ask you a question with tappable button options
• create or change the current group's icon
• create and start another conversational topic
• start or stop a detached worker
• safely tear down its current managed topic

Ask for a new topic by name and include the task you want it to start. The new topic inherits the group's provider, model, and effort unless you choose explicit overrides, receives the task as its first turn, and remains a normal conversation you can steer directly.

These skills are scoped to the active Telegram turn. They do not accept arbitrary chat or topic IDs.""",
    ),
    HelpTopic(
        "projects",
        "Projects & topics",
        """Projects and topics

Use /projects to list every connected workspace, including group-only workspaces, with its active topic and session counts. A workspace that is both enrolled in the older project catalog and bound to a group appears only once.

For an older enrolled catalog project, use /agent create project-slug in a Control-bound topic; /projects shows the available slug. Directly bound groups do not need this legacy attach step.

Setting up a new group: send /newgroup in the bot’s private chat, create a private group, enable Topics, choose Telegram’s separate “View as Topics” display mode, then tap the link. Telegram adds the bot with the rights it needs in one step. Authorize the forum, then send /bind followed by an exact existing folder path and confirm the workspace and provider.

By default, each new topic in a bound group starts immediately with the group’s provider, model, and effort. If per-topic confirmation is enabled, your first request is held while you accept those defaults or choose a different agent, then runs as the first turn—you do not resend it. Use /agent to inspect or change that topic’s conversation.

An active topic agent can create another regular conversational topic in the same group and queue a self-contained first prompt there. Ask it to create a topic by name and describe the separate task; the new independent session begins immediately and is conversational, unlike a detached worker's report-only topic.

The central conversational Control agent is optional and disabled by default. Direct mode does not need it: work happens in group topics, and a Claude-only installation does not need Codex.

Each automatically provisioned conversational topic opens with one status message, edited in place as its state changes. Type / in the paired private chat or a regular project topic for the command menu; report-only worker topics do not accept input. Topic names and Telegram coordinates remain durable routing identities until formal teardown.""",
    ),
)


TOPIC_BY_SLUG = {topic.slug: topic for topic in TOPICS}


def page_text(slug: str) -> str:
    if slug == "home":
        return HOME_TEXT
    topic = TOPIC_BY_SLUG.get(slug)
    if topic is None:
        raise ValueError("Unknown Telegram help topic.")
    return topic.text


def page_html(slug: str) -> str:
    """Return safe, controller-owned Telegram HTML for one help page."""

    return _document_html(page_text(slug))

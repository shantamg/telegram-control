"""Maintainable copy for Telegram Control's button-driven help browser."""

from __future__ import annotations

from typing import NamedTuple


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
    BotCommand("projects", "List enrolled workspaces"),
    BotCommand("newgroup", "Get the link that adds me to a new project group"),
    BotCommand("teardown", "Safely remove this managed topic and session"),
)


HOME_TEXT = (
    "Telegram Control help\n\nChoose a topic below.\n\nQuick commands:\n"
    + "\n".join(
        f"/{command.command} — {command.description}" for command in COMMANDS
    )
)


TOPICS = (
    HelpTopic(
        "agents",
        "Agents & sessions",
        """Agents and sessions

Use /agent inside a managed topic to inspect its provider, model, effort, session, console, and context usage.

The /agent controls can change the current model and effort while preserving the existing conversation, pause or resume the agent, start a fresh session, resume a previous session, or switch providers. Codex and Claude each expose their own supported choices. Reconfiguration waits for an active turn or console to become idle. Ordinary messages in the topic go to its bound agent.""",
    ),
    HelpTopic(
        "detached",
        "Detached workers",
        """Detached workers

Ask the main topic agent to start long-running work in a detached session. The worker receives its own report-only topic in the same group and can post text or voice milestones there.

Telegram Control gives the worker a durable recovery file and requires it to keep that file current whenever it creates goals, native wakeups, scheduled tasks, background agents, monitors, or processes that would need to be restored. After a reboot and graphical login, Control recreates tmux, resumes the exact provider conversation, asks it to read that file and reactivate its own native work, then reports whether the agent verified recovery.

Messages in a report-only topic do not steer the worker. Return to the main agent topic to guide or stop it. Stopping removes the worker's managed recovery file; deleting its report topic remains a separate explicit choice.""",
    ),
    HelpTopic(
        "teardown",
        "Topic teardown",
        """Safe topic teardown

Type /teardown inside an active managed agent topic. Control opens the confirmation card directly without invoking Codex or Claude.

After confirmation, Telegram Control clears the provider-session pointer, archives the agent binding, revokes routes and buttons, and removes the Telegram topic. Running detached workers that originated here must be stopped first.""",
    ),
    HelpTopic(
        "voice",
        "Voice & replies",
        """Voice messages and replies

Send a Telegram photo or any document normally, with an optional caption. Attachments up to 20 MB are saved privately and routed to Codex or Claude by local path.

Send a Telegram voice note normally; it is transcribed locally and routed like text. If Telegram briefly fails to provide the audio file, Control retries behind the same transcribing receipt and shows an error only after every durable attempt fails.

Reply to an exact Control or agent message to continue that routed conversation. During an active turn, a reply to its progress card can steer the work, and its Stop button interrupts the provider. If the owning worker has already exited, Stop clears the orphaned turn immediately so the next queued message can start. Agents can also send requested voice or text progress updates through scoped Telegram skills.""",
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
• start or stop a detached worker
• safely tear down its current managed topic

These skills are scoped to the active Telegram turn. They do not accept arbitrary chat or topic IDs.""",
    ),
    HelpTopic(
        "projects",
        "Projects & topics",
        """Projects and topics

Use /projects to list enrolled workspaces. Inside an eligible project topic, /agent create <slug> attaches its managed agent.

Setting up a new group: send /newgroup in the main Control chat, create a private group, turn on Topics, then tap the link — Telegram adds the bot with the rights it needs in one step. Answer its question about which folder the group works in with a path or a description, and confirm the binding with one button.

Each new topic in a bound group then starts with the group's own provider, model, and effort in one tap; "Choose a different agent…" opens the per-topic menus. Whatever you already sent runs as the topic's first turn, so you never resend it.

Every topic opens with one message listing its agent, model, effort, and context used. It is edited in place as those change, so it always shows the current state. Type / in any chat or topic for the command menu. Topic names and Telegram coordinates remain durable routing identities until formal teardown.""",
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

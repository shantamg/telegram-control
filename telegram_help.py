"""Maintainable copy for Telegram Control's button-driven help browser."""

from __future__ import annotations

from typing import NamedTuple


HELP_HINT = "Type /help to browse Telegram Control help."


class HelpTopic(NamedTuple):
    slug: str
    label: str
    text: str


HOME_TEXT = """Telegram Control help

Choose a topic below.

Quick commands:
/help — browse this guide
/agent — inspect or manage this topic's agent
/status — inspect this Telegram surface
/projects — list enrolled projects"""


TOPICS = (
    HelpTopic(
        "agents",
        "Agents & sessions",
        """Agents and sessions

Use /agent inside a managed topic to inspect its provider, model, effort, session, console, and context usage.

The /agent controls can pause or resume the agent, start a fresh session, resume a previous session, or switch providers. Ordinary messages in the topic go to its bound agent.""",
    ),
    HelpTopic(
        "detached",
        "Detached workers",
        """Detached workers

Ask the main topic agent to start long-running work in a detached session. The worker receives its own report-only topic in the same group and can post text or voice milestones there.

Messages in a report-only topic do not steer the worker. Return to the main agent topic to guide or stop it.""",
    ),
    HelpTopic(
        "teardown",
        "Topic teardown",
        """Safe topic teardown

Ask the current managed agent to tear down this topic. It will post a confirmation card before anything destructive happens.

After confirmation, Telegram Control clears the provider-session pointer, archives the agent binding, revokes routes and buttons, and removes the Telegram topic. Running detached workers that originated here must be stopped first.""",
    ),
    HelpTopic(
        "voice",
        "Voice & replies",
        """Voice messages and replies

Send a Telegram voice note normally; it is transcribed locally and routed like text.

Reply to an exact Control or agent message to continue that routed conversation. During an active turn, a reply to its progress card can steer or stop the work. Agents can also send requested voice or text progress updates through scoped Telegram skills.""",
    ),
    HelpTopic(
        "skills",
        "Telegram skills",
        """Telegram-specific skills

You can ask an active managed agent to:
• send a separate text progress update
• reply with a Telegram voice note
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

In a bound private forum, each new topic can choose Codex or Claude, then its model and effort. Topic names and Telegram coordinates remain durable routing identities until formal teardown.""",
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

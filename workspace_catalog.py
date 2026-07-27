"""Path-safe rendering for the unified workspace inventory."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import html
from typing import Optional

from durable_store import WorkspaceInventoryEntry


def _count_text(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def _escape(value: str) -> str:
    return html.escape(str(value), quote=False)


def render_workspace_catalog(
    entries: Iterable[WorkspaceInventoryEntry],
    aliases: Optional[Mapping[str, list[str]]] = None,
) -> str:
    """Render path-free Telegram HTML for connected workspaces."""
    inventory = list(entries)
    if not inventory:
        return "<b>No workspaces are connected yet.</b>"

    alias_map = aliases or {}
    total_topics = sum(entry.active_topic_count for entry in inventory)
    total_sessions = sum(entry.active_session_count for entry in inventory)
    lines = [
        "<b>Connected workspaces</b>",
        " · ".join(
            [
                _count_text(len(inventory), "workspace", "workspaces"),
                _count_text(total_topics, "active topic", "active topics"),
                _count_text(total_sessions, "session", "sessions"),
            ]
        ),
        "",
    ]
    for entry in inventory:
        providers = " / ".join(
            provider.title() for provider in entry.providers
        )
        lines.append(
            f"<b>{_escape(entry.display_name)}</b> · "
            f"<code>{_escape(providers)}</code>"
        )

        status: list[str] = []
        if entry.forum_names:
            status.extend(
                [
                    _count_text(
                        entry.active_topic_count,
                        "active topic",
                        "active topics",
                    ),
                    _count_text(
                        entry.active_session_count,
                        "session",
                        "sessions",
                    ),
                ]
            )
            if (
                len(entry.forum_names) != 1
                or entry.forum_names[0] != entry.display_name
            ):
                label = "Group" if len(entry.forum_names) == 1 else "Groups"
                status.append(
                    f"{label}: "
                    + ", ".join(_escape(name) for name in entry.forum_names)
                )
        elif entry.project_agent_state is not None:
            status.append(
                "Project agent: "
                + _escape(entry.project_agent_state.replace("_", " "))
            )
        if status:
            lines.append(" · ".join(status))

        identity: list[str] = []
        if entry.project_slug is not None:
            identity.append(
                f"Slug: <code>{_escape(entry.project_slug)}</code>"
            )
        project_aliases = (
            alias_map.get(entry.project_slug, [])
            if entry.project_slug is not None
            else []
        )
        if project_aliases:
            alias_label = "Alias" if len(project_aliases) == 1 else "Aliases"
            identity.append(
                f"{alias_label}: "
                + ", ".join(
                    f"<code>{_escape(alias)}</code>"
                    for alias in project_aliases
                )
            )
        if identity:
            lines.append(" · ".join(identity))
        lines.append("")

    return "\n".join(lines).rstrip()

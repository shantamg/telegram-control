"""Path-safe rendering for the unified workspace inventory."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Optional

from durable_store import WorkspaceInventoryEntry


def _count_text(count: int, singular: str, plural: str) -> str:
    return f"{count} {singular if count == 1 else plural}"


def render_workspace_catalog(
    entries: Iterable[WorkspaceInventoryEntry],
    aliases: Optional[Mapping[str, list[str]]] = None,
) -> str:
    """Render connected workspaces without exposing filesystem coordinates."""
    inventory = list(entries)
    if not inventory:
        return "No workspaces are connected yet."

    alias_map = aliases or {}
    lines = ["Connected workspaces", ""]
    for entry in inventory:
        providers = " / ".join(entry.providers)
        if entry.project_slug is not None:
            heading = (
                f"{entry.project_slug} — {entry.display_name} ({providers})"
            )
        else:
            heading = f"{entry.display_name} ({providers})"

        if not entry.forum_names and entry.project_agent_state is not None:
            heading += f" · {entry.project_agent_state.replace('_', ' ')}"
        lines.append(heading)

        details: list[str] = []
        if entry.forum_names:
            if (
                len(entry.forum_names) == 1
                and entry.forum_names[0] == entry.display_name
            ):
                details.append("Bound group")
            elif len(entry.forum_names) == 1:
                details.append(f"Group: {entry.forum_names[0]}")
            else:
                details.append("Groups: " + ", ".join(entry.forum_names))
            details.extend(
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
            entry.project_agent_state not in {None, "not_created"}
            and entry.forum_names
        ):
            details.append(
                "Project agent: "
                + entry.project_agent_state.replace("_", " ")
            )
        if details:
            lines.append("  " + " · ".join(details))

        project_aliases = (
            alias_map.get(entry.project_slug, [])
            if entry.project_slug is not None
            else []
        )
        if project_aliases:
            lines.append("  Aliases: " + ", ".join(project_aliases))

    return "\n".join(lines)

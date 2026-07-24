"""Read-only discovery of locally persisted Codex sessions."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


SESSION_ID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MAX_SESSION_FILES = 800
MAX_DIRECTORY_ENTRIES = 2_500
MAX_METADATA_BYTES = 64 * 1024
MAX_INDEX_BYTES = 512 * 1024
MAX_INDEX_LINES = 2_000
MAX_SCAN_SECONDS = 1.5
MAX_DATE_DIRECTORY_ENTRIES = 1_024
MAX_DAY_DIRECTORIES = 120


@dataclass(frozen=True)
class CodexSession:
    session_id: str
    working_directory: str
    originator: str
    updated_at: float
    title: Optional[str] = None

    def button_label(self) -> str:
        when = datetime.fromtimestamp(self.updated_at).strftime("%b %-d, %-I:%M %p")
        title = compact_label(self.title or self.originator or "Codex session", 34)
        return f"{when} · {title}"


def compact_label(value: str, limit: int) -> str:
    safe = "".join(
        character
        for character in str(value)
        if ord(character) >= 32 and ord(character) != 127
    )
    text = " ".join(safe.split()) or "Codex session"
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _session_titles(
    index_path: Path,
    wanted_session_ids: set[str],
) -> dict[str, str]:
    titles: dict[str, str] = {}
    if not wanted_session_ids:
        return titles
    try:
        with index_path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            offset = max(0, size - MAX_INDEX_BYTES)
            handle.seek(offset)
            data = handle.read(MAX_INDEX_BYTES)
        if offset:
            _, separator, data = data.partition(b"\n")
            if not separator:
                return titles
        lines = data.splitlines()[-MAX_INDEX_LINES:]
        for raw_line in lines:
            if len(raw_line) > MAX_METADATA_BYTES:
                continue
            try:
                line = raw_line.decode("utf-8", errors="replace")
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError, UnicodeError):
                continue
            session_id = record.get("id")
            title = record.get("thread_name")
            if (
                isinstance(session_id, str)
                and session_id in wanted_session_ids
                and isinstance(title, str)
                and title.strip()
            ):
                titles[session_id] = title.strip()
    except OSError:
        pass
    return titles


def _recent_session_paths(
    root: Path,
    *,
    deadline: float,
) -> list[Path]:
    """Enumerate a capped newest-first slice without opening session bodies."""
    day_directories: list[Path] = []
    date_entries_seen = 0

    def child_directories(parent: Path) -> list[Path]:
        nonlocal date_entries_seen
        children: list[Path] = []
        try:
            with os.scandir(parent) as entries:
                for entry in entries:
                    date_entries_seen += 1
                    if entry.is_dir(follow_symlinks=False):
                        children.append(parent / entry.name)
                    if (
                        date_entries_seen >= MAX_DATE_DIRECTORY_ENTRIES
                        or time.monotonic() >= deadline
                    ):
                        break
        except OSError:
            return []
        return sorted(children, reverse=True)

    for year in child_directories(root):
        for month in child_directories(year):
            for day in child_directories(month):
                day_directories.append(day)
                if (
                    len(day_directories) >= MAX_DAY_DIRECTORIES
                    or date_entries_seen >= MAX_DATE_DIRECTORY_ENTRIES
                    or time.monotonic() >= deadline
                ):
                    break
            if len(day_directories) >= MAX_DAY_DIRECTORIES:
                break
        if len(day_directories) >= MAX_DAY_DIRECTORIES:
            break
    day_directories.sort(reverse=True)
    paths: list[Path] = []
    entries_seen = 0
    for directory in day_directories:
        if (
            len(paths) >= MAX_SESSION_FILES
            or entries_seen >= MAX_DIRECTORY_ENTRIES
            or time.monotonic() >= deadline
        ):
            break
        names: list[str] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entry.is_file(follow_symlinks=False) and entry.name.endswith(
                        ".jsonl"
                    ):
                        names.append(entry.name)
                    if (
                        entries_seen >= MAX_DIRECTORY_ENTRIES
                        or time.monotonic() >= deadline
                    ):
                        break
        except OSError:
            continue
        for name in sorted(names, reverse=True):
            paths.append(directory / name)
            if len(paths) >= MAX_SESSION_FILES:
                break
    return paths


def _read_first_record(path: Path) -> Optional[dict]:
    try:
        with path.open("rb") as handle:
            data = handle.readline(MAX_METADATA_BYTES + 1)
        if len(data) > MAX_METADATA_BYTES or not data.endswith(b"\n"):
            return None
        record = json.loads(data.decode("utf-8", errors="replace"))
        return record if isinstance(record, dict) else None
    except (OSError, json.JSONDecodeError, TypeError, UnicodeError):
        return None


def _metadata_from_file(
    path: Path,
    titles: Optional[dict[str, str]] = None,
) -> Optional[CodexSession]:
    try:
        record = _read_first_record(path)
        if record is None or record.get("type") != "session_meta":
            return None
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return None
        session_id = payload.get("id") or payload.get("session_id")
        working_directory = payload.get("cwd")
        originator = payload.get("originator")
        if (
            not isinstance(session_id, str)
            or SESSION_ID_PATTERN.fullmatch(session_id) is None
            or not isinstance(working_directory, str)
            or not working_directory
        ):
            return None
        return CodexSession(
            session_id=session_id,
            working_directory=os.path.realpath(working_directory),
            originator=str(originator or "Codex"),
            updated_at=path.stat().st_mtime,
            title=(titles or {}).get(session_id),
        )
    except (OSError, TypeError, ValueError):
        return None


def discover_sessions(
    working_directory: str,
    *,
    sessions_root: Optional[Path] = None,
    excluded_session_ids: Iterable[str] = (),
    limit: int = 5,
) -> list[CodexSession]:
    """Return recent non-controller sessions for exactly one working directory."""
    if limit <= 0:
        return []
    root = sessions_root or (Path.home() / ".codex" / "sessions")
    target = os.path.realpath(working_directory)
    excluded = set(excluded_session_ids)
    candidates: list[CodexSession] = []
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    for path in _recent_session_paths(root, deadline=deadline):
        if time.monotonic() >= deadline:
            break
        session = _metadata_from_file(path)
        if (
            session is None
            or session.working_directory != target
            or session.session_id in excluded
            or session.originator == "telegram-control"
        ):
            continue
        candidates.append(session)
    candidates.sort(key=lambda candidate: candidate.updated_at, reverse=True)
    candidates = candidates[:limit]
    titles = _session_titles(
        root.parent / "session_index.jsonl",
        {candidate.session_id for candidate in candidates},
    )
    return [
        CodexSession(
            session_id=candidate.session_id,
            working_directory=candidate.working_directory,
            originator=candidate.originator,
            updated_at=candidate.updated_at,
            title=titles.get(candidate.session_id),
        )
        for candidate in candidates
    ]


def resolve_session(
    session_id: str,
    working_directory: str,
    *,
    sessions_root: Optional[Path] = None,
) -> Optional[CodexSession]:
    """Revalidate a selected persisted session against its exact directory."""
    if SESSION_ID_PATTERN.fullmatch(str(session_id)) is None:
        return None
    root = sessions_root or (Path.home() / ".codex" / "sessions")
    target = os.path.realpath(working_directory)
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    for path in _recent_session_paths(root, deadline=deadline):
        if time.monotonic() >= deadline:
            break
        if not path.name.endswith(f"{session_id}.jsonl"):
            continue
        session = _metadata_from_file(path)
        if (
            session is not None
            and session.session_id == session_id
            and session.working_directory == target
            and session.originator != "telegram-control"
        ):
            titles = _session_titles(
                root.parent / "session_index.jsonl",
                {session_id},
            )
            return CodexSession(
                session_id=session.session_id,
                working_directory=session.working_directory,
                originator=session.originator,
                updated_at=session.updated_at,
                title=titles.get(session_id),
            )
    return None

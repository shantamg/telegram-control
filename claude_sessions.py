"""Read-only discovery of locally persisted Claude Code sessions."""

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
MAX_PROJECT_ENTRIES = 800
MAX_SESSION_FILES = 800
MAX_METADATA_BYTES = 64 * 1024
MAX_SCAN_BYTES = 512 * 1024
MAX_SCAN_LINES = 64
MAX_SCAN_SECONDS = 1.5


@dataclass(frozen=True)
class ClaudeSession:
    session_id: str
    working_directory: str
    originator: str
    updated_at: float
    title: Optional[str] = None

    def button_label(self) -> str:
        when = datetime.fromtimestamp(self.updated_at).strftime("%b %-d, %-I:%M %p")
        title = compact_label(self.title or self.originator or "Claude session", 34)
        return f"{when} · {title}"


def compact_label(value: str, limit: int) -> str:
    safe = "".join(
        character
        for character in str(value)
        if ord(character) >= 32 and ord(character) != 127
    )
    text = " ".join(safe.split()) or "Claude session"
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _encoded_project_name(working_directory: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(working_directory))


def _index_matches(index_path: Path, target: str) -> bool:
    try:
        if index_path.stat().st_size > MAX_SCAN_BYTES:
            return False
        record = json.loads(index_path.read_text(encoding="utf-8"))
        if not isinstance(record, dict):
            return False
        original_path = record.get("originalPath")
        if isinstance(original_path, str) and os.path.realpath(original_path) == target:
            return True
        entries = record.get("entries")
        if not isinstance(entries, list):
            return False
        for entry in entries[:MAX_SCAN_LINES]:
            if not isinstance(entry, dict):
                continue
            project_path = entry.get("projectPath")
            if (
                isinstance(project_path, str)
                and os.path.realpath(project_path) == target
            ):
                return True
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return False


def _project_directories(
    projects_root: Path,
    target: str,
    *,
    deadline: float,
) -> list[Path]:
    """Find likely storage directories without recursively scanning ~/.claude."""
    matches: list[Path] = []
    direct = projects_root / _encoded_project_name(target)
    if direct.is_dir():
        matches.append(direct)
    try:
        with os.scandir(projects_root) as entries:
            for index, entry in enumerate(entries):
                if index >= MAX_PROJECT_ENTRIES or time.monotonic() >= deadline:
                    break
                if not entry.is_dir(follow_symlinks=False):
                    continue
                candidate = projects_root / entry.name
                if candidate == direct:
                    continue
                if _index_matches(candidate / "sessions-index.json", target):
                    matches.append(candidate)
    except OSError:
        pass
    return matches


def _recent_session_paths(
    directories: Iterable[Path],
    *,
    deadline: float,
) -> list[Path]:
    paths: list[tuple[float, Path]] = []
    entries_seen = 0
    for directory in directories:
        if time.monotonic() >= deadline:
            break
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    entries_seen += 1
                    if entries_seen > MAX_SESSION_FILES or time.monotonic() >= deadline:
                        break
                    if (
                        not entry.is_file(follow_symlinks=False)
                        or not entry.name.endswith(".jsonl")
                    ):
                        continue
                    session_id = entry.name[:-6]
                    if SESSION_ID_PATTERN.fullmatch(session_id) is None:
                        continue
                    try:
                        paths.append((entry.stat(follow_symlinks=False).st_mtime, Path(entry.path)))
                    except OSError:
                        continue
        except OSError:
            continue
    paths.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in paths]


def _user_text(record: dict) -> Optional[str]:
    if record.get("type") != "user" or record.get("isMeta") is True:
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    values: list[str] = []
    if isinstance(content, str):
        values.append(content)
    elif isinstance(content, list):
        for part in content:
            if (
                isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ):
                values.append(part["text"])
    for value in values:
        text = value.strip()
        if text and not text.startswith("<local-command-") and not text.startswith(
            "<command-"
        ):
            return text
    return None


def _metadata_from_file(path: Path) -> Optional[ClaudeSession]:
    session_id = path.stem
    if SESSION_ID_PATTERN.fullmatch(session_id) is None:
        return None
    working_directory: Optional[str] = None
    originator = "Claude Code"
    title: Optional[str] = None
    bytes_read = 0
    try:
        with path.open("rb") as handle:
            for _ in range(MAX_SCAN_LINES):
                remaining = MAX_SCAN_BYTES - bytes_read
                if remaining <= 0:
                    break
                raw_line = handle.readline(min(MAX_METADATA_BYTES + 1, remaining + 1))
                if not raw_line:
                    break
                bytes_read += len(raw_line)
                if (
                    len(raw_line) > MAX_METADATA_BYTES
                    or bytes_read > MAX_SCAN_BYTES
                    or not raw_line.endswith(b"\n")
                ):
                    continue
                try:
                    record = json.loads(raw_line.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, TypeError, UnicodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                record_session_id = record.get("sessionId")
                if (
                    record_session_id is not None
                    and record_session_id != session_id
                ):
                    return None
                cwd = record.get("cwd")
                if isinstance(cwd, str) and cwd:
                    working_directory = os.path.realpath(cwd)
                entrypoint = record.get("entrypoint")
                if entrypoint == "cli":
                    originator = "Claude Code"
                if title is None:
                    title = _user_text(record)
                if working_directory is not None and title is not None:
                    break
        if working_directory is None:
            return None
        return ClaudeSession(
            session_id=session_id,
            working_directory=working_directory,
            originator=originator,
            updated_at=path.stat().st_mtime,
            title=title,
        )
    except (OSError, TypeError, ValueError):
        return None


def discover_sessions(
    working_directory: str,
    *,
    projects_root: Optional[Path] = None,
    excluded_session_ids: Iterable[str] = (),
    limit: int = 5,
) -> list[ClaudeSession]:
    """Return recent Claude sessions for exactly one working directory."""
    if limit <= 0:
        return []
    root = projects_root or (Path.home() / ".claude" / "projects")
    target = os.path.realpath(working_directory)
    excluded = set(excluded_session_ids)
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    directories = _project_directories(root, target, deadline=deadline)
    sessions: list[ClaudeSession] = []
    for path in _recent_session_paths(directories, deadline=deadline):
        if time.monotonic() >= deadline:
            break
        session = _metadata_from_file(path)
        if (
            session is None
            or session.working_directory != target
            or session.session_id in excluded
        ):
            continue
        sessions.append(session)
        if len(sessions) >= limit:
            break
    return sessions


def resolve_session(
    session_id: str,
    working_directory: str,
    *,
    projects_root: Optional[Path] = None,
) -> Optional[ClaudeSession]:
    """Revalidate a selected Claude session against its exact directory."""
    if SESSION_ID_PATTERN.fullmatch(str(session_id)) is None:
        return None
    root = projects_root or (Path.home() / ".claude" / "projects")
    target = os.path.realpath(working_directory)
    deadline = time.monotonic() + MAX_SCAN_SECONDS
    directories = _project_directories(root, target, deadline=deadline)
    for directory in directories:
        if time.monotonic() >= deadline:
            break
        path = directory / f"{session_id}.jsonl"
        session = _metadata_from_file(path)
        if (
            session is not None
            and session.session_id == session_id
            and session.working_directory == target
        ):
            return session
    return None

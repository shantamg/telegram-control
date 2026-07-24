#!/usr/bin/python3
"""Bounded read-only filesystem discovery for the conversational Control agent.

Every function here is strictly read-only and bounded: discovery may only
report directories whose resolved real paths stay inside the user-authorized
discovery roots, hidden directories are skipped, walks have hard depth and
visit limits, and result lists are capped. Symlinks are followed only when
their targets remain inside the roots, so discovery can never escape them.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import telegram_bridge as bridge
from durable_store import StoreError, validate_workspace_paths


DISCOVERY_MAX_DEPTH = 4
DISCOVERY_MAX_RESULTS = 8
DISCOVERY_MAX_SUBDIRECTORIES = 25
DISCOVERY_MAX_VISITED = 5000
DISCOVERY_MAX_SCANNED_ENTRIES = 20_000
DISCOVERY_MAX_SCAN_SECONDS = 10.0
SKIPPED_DIRECTORY_NAMES = {
    "node_modules",
    "__pycache__",
    "venv",
    "Library",
    "Applications",
    "Movies",
    "Music",
    "Pictures",
}


def load_discovery_roots() -> list[Path]:
    """Return the user-authorized discovery roots as resolved real paths."""
    configured: list[str] = []
    try:
        config = bridge.load_config()
        raw = config.get("discovery_roots")
        if isinstance(raw, list):
            configured = [str(item) for item in raw if str(item)]
    except bridge.BridgeError:
        configured = []
    if not configured:
        configured = [str(Path.home())]
    roots = []
    for item in configured:
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            continue
        real = Path(os.path.realpath(candidate))
        if real.is_dir():
            roots.append(real)
    if not roots:
        raise StoreError("No usable discovery roots are configured.")
    return roots


def _within_roots(real_path: Path, roots: list[Path]) -> bool:
    return any(
        real_path == root or root in real_path.parents for root in roots
    )


def within_roots(path_text: str, roots: list[Path]) -> bool:
    """Public containment check for controller-side ref issuance."""
    return _within_roots(Path(os.path.realpath(path_text)), roots)


def _effective_deadline(deadline: Optional[float]) -> float:
    local_deadline = time.monotonic() + DISCOVERY_MAX_SCAN_SECONDS
    return min(local_deadline, deadline) if deadline is not None else local_deadline


def _git_root_of(
    path: Path,
    deadline: Optional[float] = None,
) -> Optional[str]:
    remaining = (
        5.0
        if deadline is None
        else min(5.0, deadline - time.monotonic())
    )
    if remaining <= 0:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return str(Path(os.path.realpath(top))) if top else None


def _normalized_name(name: str) -> str:
    return re.sub(r"[-_.]+", " ", name).casefold().strip()


def _query_tokens(query: str) -> list[str]:
    return [
        token
        for token in re.split(r"[^a-z0-9]+", query.casefold())
        if token
    ]


def _name_matches(name: str, tokens: list[str]) -> bool:
    normalized = _normalized_name(name)
    return all(token in normalized for token in tokens)


def _match_rank(name: str, query: str) -> int:
    normalized = _normalized_name(name)
    normalized_query = _normalized_name(query)
    if normalized == normalized_query:
        return 0
    if normalized.startswith(normalized_query):
        return 1
    return 2


def _describe_directory(
    real_path: Path,
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    git_root = _git_root_of(real_path, deadline)
    return {
        "path": str(real_path),
        "name": real_path.name,
        "is_git_root": git_root == str(real_path),
        "containing_git_root": git_root,
    }


def find_directory(
    query: str,
    roots: list[Path],
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    """Search the authorized roots for directories matching a description."""
    text = str(query).strip()
    if not text or len(text) > 200:
        raise StoreError("Discovery query must contain 1 to 200 characters.")
    tokens = _query_tokens(text)
    if not tokens:
        raise StoreError("Discovery query has no searchable words.")
    matches: list[Path] = []
    visited = 0
    scanned_entries = 0
    truncated = False
    scan_deadline = _effective_deadline(deadline)
    seen_real: set[str] = set()
    queue: list[tuple[Path, int]] = [(root, 0) for root in roots]
    while queue:
        directory, depth = queue.pop(0)
        directory_key = str(directory)
        if directory_key in seen_real:
            # Symlink cycles must not loop the walk.
            continue
        seen_real.add(directory_key)
        visited += 1
        if visited > DISCOVERY_MAX_VISITED or (
            time.monotonic() > scan_deadline
        ):
            truncated = True
            break
        child_directories: list[tuple[str, Path]] = []
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    scanned_entries += 1
                    if (
                        scanned_entries > DISCOVERY_MAX_SCANNED_ENTRIES
                        or time.monotonic() >= scan_deadline
                    ):
                        truncated = True
                        queue.clear()
                        break
                    name = entry.name
                    if (
                        name.startswith(".")
                        or name in SKIPPED_DIRECTORY_NAMES
                    ):
                        continue
                    try:
                        if not entry.is_dir(follow_symlinks=True):
                            continue
                    except OSError:
                        continue
                    real = Path(os.path.realpath(Path(directory) / name))
                    if not _within_roots(real, roots):
                        continue
                    child_directories.append((name, real))
        except OSError:
            continue
        for name, real in sorted(
            child_directories,
            key=lambda item: item[0].casefold(),
        ):
            if time.monotonic() >= scan_deadline:
                truncated = True
                queue.clear()
                break
            if _name_matches(name, tokens) and real not in matches:
                matches.append(real)
                if len(matches) >= DISCOVERY_MAX_RESULTS * 3:
                    truncated = True
                    queue.clear()
                    break
            if depth + 1 < DISCOVERY_MAX_DEPTH:
                queue.append((real, depth + 1))
    ranked = sorted(
        matches,
        key=lambda path: (_match_rank(path.name, text), len(str(path))),
    )
    if len(ranked) > DISCOVERY_MAX_RESULTS:
        ranked = ranked[:DISCOVERY_MAX_RESULTS]
        truncated = True
    return {
        "query": text,
        "candidates": [
            _describe_directory(path, scan_deadline) for path in ranked
        ],
        "truncated": truncated,
        "scanned_entries": min(
            scanned_entries,
            DISCOVERY_MAX_SCANNED_ENTRIES,
        ),
    }


def inspect_directory(
    path_text: str,
    roots: list[Path],
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    """Inspect one directory inside the authorized roots, read-only."""
    text = str(path_text).strip()
    if not text or len(text) > 500:
        raise StoreError("Discovery path must contain 1 to 500 characters.")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise StoreError("Discovery paths must be absolute.")
    real = Path(os.path.realpath(candidate))
    if not _within_roots(real, roots):
        raise StoreError(
            "That path is outside the authorized discovery locations."
        )
    containing_root = next(
        root for root in roots if real == root or root in real.parents
    )
    relative_parts = real.relative_to(containing_root).parts
    if any(
        part.startswith(".") or part in SKIPPED_DIRECTORY_NAMES
        for part in relative_parts
    ):
        raise StoreError(
            "Hidden or excluded directories cannot be inspected."
        )
    if not real.exists():
        return {"path": str(real), "exists": False, "is_directory": False}
    if not real.is_dir():
        return {"path": str(real), "exists": True, "is_directory": False}
    scan_deadline = _effective_deadline(deadline)
    description = _describe_directory(real, scan_deadline)
    subdirectories = []
    truncated = False
    scanned_entries = 0
    try:
        entries = os.scandir(real)
    except OSError:
        entries = None
    if entries is not None:
        with entries:
            for entry in entries:
                scanned_entries += 1
                if (
                    scanned_entries > DISCOVERY_MAX_SCANNED_ENTRIES
                    or time.monotonic() >= scan_deadline
                ):
                    truncated = True
                    break
                name = entry.name
                if (
                    name.startswith(".")
                    or name in SKIPPED_DIRECTORY_NAMES
                ):
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=True):
                        continue
                except OSError:
                    continue
                entry_real = Path(os.path.realpath(Path(real) / name))
                if not _within_roots(entry_real, roots):
                    continue
                if len(subdirectories) >= DISCOVERY_MAX_SUBDIRECTORIES:
                    truncated = True
                    break
                subdirectories.append(name)
    subdirectories.sort(key=str.casefold)
    description.update(
        {
            "exists": True,
            "is_directory": True,
            "subdirectories": subdirectories,
            "truncated": truncated,
        }
    )
    return description


def validate_repository_workspace(
    repository_root: str,
    working_directory: Optional[str] = None,
) -> tuple[str, str]:
    """Fully validate a repository root and working directory pair.

    Resolves both paths through symlinks, requires containment, and requires
    the resolved root to be an actual Git repository root. Used identically
    at proposal time, confirmation time (TOCTOU re-check), and agent launch,
    so no step can rely on stale filesystem state.
    """
    root_real, workdir_real = validate_workspace_paths(
        repository_root,
        working_directory,
    )
    git_root = _git_root_of(Path(root_real))
    if git_root != root_real:
        raise StoreError(
            "The repository root is not the root of a Git repository."
        )
    return root_real, workdir_real


def execute_discovery_tool(
    tool: str,
    arguments: dict[str, Any],
    roots: list[Path],
    deadline: Optional[float] = None,
) -> dict[str, Any]:
    if tool == "find_directory":
        return find_directory(
            str(arguments.get("query", "")),
            roots,
            deadline,
        )
    if tool == "inspect_directory":
        return inspect_directory(
            str(arguments.get("path", "")),
            roots,
            deadline,
        )
    raise StoreError(f"Unknown discovery tool: {tool}")

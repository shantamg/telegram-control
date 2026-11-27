"""Locate the optional local helper binaries the controller shells out to.

Voice input and voice output depend on binaries whose location differs per
machine — Homebrew alone installs to `/opt/homebrew` on Apple silicon and
`/usr/local` on Intel — so nothing here may assume one layout. Each binary is
resolved from, in order:

1. an explicit absolute path in `config.json` (for example `"ffmpeg_binary"`),
2. the documented default locations, and
3. the command name on `PATH`.

When none of those exist the first documented default is returned unchanged, so
callers keep reporting a concrete, actionable location in their error message.
This module deliberately imports nothing from the rest of the project: it is
loaded by both the message handler and the voice responder.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Optional


IS_LINUX = sys.platform.startswith("linux")
if IS_LINUX:
    CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "telegram-bridge"
    SPEECH_RUNTIME_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "telegram-control" / "speech"
else:
    CONFIG_DIR = Path.home() / "Library" / "Application Support" / "telegram-bridge"
CONFIG_PATH = CONFIG_DIR / "config.json"


def configured_binary(config_key: str) -> Optional[Path]:
    """Read an absolute helper path override from the local configuration."""
    try:
        config = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(config, dict):
        return None
    raw = config.get(config_key)
    if not isinstance(raw, str) or not raw.strip():
        return None
    candidate = Path(raw.strip()).expanduser()
    if not candidate.is_absolute():
        return None
    return candidate


def _usable(candidate: Path) -> bool:
    return candidate.is_file() and os.access(candidate, os.X_OK)


def resolve_binary(
    config_key: str,
    *defaults: Path,
    command_name: Optional[str] = None,
) -> Path:
    """Find a helper binary, falling back to the first documented default."""
    if not defaults:
        raise ValueError("At least one default helper location is required.")
    override = configured_binary(config_key)
    if override is not None:
        return override
    for candidate in defaults:
        if _usable(candidate):
            return candidate
    if command_name:
        found = shutil.which(command_name)
        if found:
            return Path(found)
    return defaults[0]

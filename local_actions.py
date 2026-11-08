"""Run explicitly allowlisted local commands from durable Telegram buttons."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DEFAULT_CONFIG_PATH = (
    Path.home() / ".config" / "telegram-control" / "local-actions.json"
)
KEY_PATTERN = re.compile(r"[a-z][a-z0-9-]{1,63}")
MAX_OUTPUT_CHARACTERS = 3_500


class LocalActionError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalAction:
    key: str
    argv: tuple[str, ...]
    working_directory: Path
    timeout_seconds: float


def load_local_action(
    key: str,
    *,
    config_path: Optional[Path] = None,
) -> LocalAction:
    if not KEY_PATTERN.fullmatch(key):
        raise LocalActionError("Local action key is invalid.")
    path = (config_path or DEFAULT_CONFIG_PATH).expanduser()
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise LocalActionError("Local action configuration is missing.") from exc
    if metadata.st_uid != os.getuid():
        raise LocalActionError("Local action configuration has the wrong owner.")
    if stat.S_IMODE(metadata.st_mode) & 0o022:
        raise LocalActionError(
            "Local action configuration must not be group- or world-writable."
        )
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalActionError("Local action configuration is unreadable.") from exc
    actions = payload.get("actions")
    if not isinstance(actions, dict) or not isinstance(actions.get(key), dict):
        raise LocalActionError("Local action is not allowlisted.")
    configured = actions[key]
    argv = configured.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(value, str) and value for value in argv)
    ):
        raise LocalActionError("Local action argv must be a non-empty string list.")
    executable = Path(argv[0])
    if not executable.is_absolute() or not executable.is_file():
        raise LocalActionError("Local action executable must be an absolute file.")
    working_directory = Path(
        str(configured.get("working_directory", Path.home()))
    ).expanduser()
    if not working_directory.is_absolute() or not working_directory.is_dir():
        raise LocalActionError(
            "Local action working directory must be an absolute directory."
        )
    try:
        timeout_seconds = float(configured.get("timeout_seconds", 60))
    except (TypeError, ValueError) as exc:
        raise LocalActionError("Local action timeout is invalid.") from exc
    if not 1 <= timeout_seconds <= 120:
        raise LocalActionError(
            "Local action timeout must be between 1 and 120 seconds."
        )
    return LocalAction(
        key=key,
        argv=tuple(argv),
        working_directory=working_directory,
        timeout_seconds=timeout_seconds,
    )


def run_local_action(
    key: str,
    *,
    config_path: Optional[Path] = None,
) -> str:
    action = load_local_action(key, config_path=config_path)
    try:
        completed = subprocess.run(
            action.argv,
            cwd=action.working_directory,
            env={
                "HOME": str(Path.home()),
                "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=action.timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise LocalActionError("Local action timed out.") from exc
    except OSError as exc:
        raise LocalActionError("Local action could not start.") from exc
    if completed.returncode != 0:
        raise LocalActionError(
            f"Local action failed with exit code {completed.returncode}."
        )
    output = completed.stdout.strip()
    if not output:
        output = "✅ Local action completed."
    if len(output) > MAX_OUTPUT_CHARACTERS:
        output = output[: MAX_OUTPUT_CHARACTERS - 1] + "…"
    return output

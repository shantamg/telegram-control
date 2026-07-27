"""Validated, layered settings for Telegram Control.

Pairing credentials and machine paths continue to live in the bridge's private
``config.json``. Product behavior lives under its ``telegram_control`` key and
may be refined by files inside an explicitly bound workspace:

1. built-in safe defaults;
2. per-install ``config.json`` settings;
3. shared ``.telegram-control.json`` workspace settings;
4. private ``.telegram-control.local.json`` workspace settings.

The local workspace file is intended to be gitignored. Security and transport
invariants are deliberately not configurable here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional


class ConfigError(ValueError):
    """A user-authored Telegram Control setting is invalid."""


DEFAULT_SETTINGS: dict[str, Any] = {
    "control_agent": {
        "enabled": False,
    },
    "topics": {
        "confirm_agent": False,
    },
    "defaults": {
        "provider": "auto",
    },
    "prompts": {
        "preamble": "",
        "preamble_file": "",
        "response_style": "",
        "response_style_file": "",
    },
    "presentation": {
        "status_style": "standard",
    },
}

WORKSPACE_CONFIG_NAME = ".telegram-control.json"
LOCAL_WORKSPACE_CONFIG_NAME = ".telegram-control.local.json"
MAX_PROMPT_TEXT = 4_000
INSTALL_CONFIG_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "telegram-bridge"
    / "config.json"
)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Could not read Telegram Control settings at {path}.") from exc
    if not isinstance(value, dict):
        raise ConfigError(f"Telegram Control settings at {path} must be an object.")
    return value


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if (
            isinstance(value, dict)
            and isinstance(result.get(key), dict)
        ):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _require_keys(
    value: dict[str, Any],
    allowed: set[str],
    location: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ConfigError(
            f"Unknown Telegram Control setting at {location}: {unknown[0]}"
        )


def validate_settings(value: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized settings object or raise a precise error."""
    if not isinstance(value, dict):
        raise ConfigError("Telegram Control settings must be an object.")
    _require_keys(value, set(DEFAULT_SETTINGS), "telegram_control")

    normalized = copy.deepcopy(DEFAULT_SETTINGS)
    merged = _merge(normalized, value)

    control = merged.get("control_agent")
    if not isinstance(control, dict):
        raise ConfigError("telegram_control.control_agent must be an object.")
    _require_keys(control, {"enabled"}, "telegram_control.control_agent")
    if not isinstance(control.get("enabled"), bool):
        raise ConfigError(
            "telegram_control.control_agent.enabled must be true or false."
        )

    topics = merged.get("topics")
    if not isinstance(topics, dict):
        raise ConfigError("telegram_control.topics must be an object.")
    _require_keys(topics, {"confirm_agent"}, "telegram_control.topics")
    if not isinstance(topics.get("confirm_agent"), bool):
        raise ConfigError(
            "telegram_control.topics.confirm_agent must be true or false."
        )

    defaults = merged.get("defaults")
    if not isinstance(defaults, dict):
        raise ConfigError("telegram_control.defaults must be an object.")
    _require_keys(defaults, {"provider"}, "telegram_control.defaults")
    provider = defaults.get("provider")
    if provider not in {"auto", "codex", "claude"}:
        raise ConfigError(
            "telegram_control.defaults.provider must be auto, codex, or claude."
        )

    prompts = merged.get("prompts")
    if not isinstance(prompts, dict):
        raise ConfigError("telegram_control.prompts must be an object.")
    _require_keys(
        prompts,
        {
            "preamble",
            "preamble_file",
            "response_style",
            "response_style_file",
        },
        "telegram_control.prompts",
    )
    for name in (
        "preamble",
        "preamble_file",
        "response_style",
        "response_style_file",
    ):
        prompt = prompts.get(name)
        if not isinstance(prompt, str) or len(prompt) > MAX_PROMPT_TEXT:
            raise ConfigError(
                f"telegram_control.prompts.{name} must be a string of at most "
                f"{MAX_PROMPT_TEXT} characters."
            )
    for name in ("preamble", "response_style"):
        if prompts[name].strip() and prompts[f"{name}_file"].strip():
            raise ConfigError(
                f"telegram_control.prompts.{name} and {name}_file cannot "
                "both be set."
            )

    presentation = merged.get("presentation")
    if not isinstance(presentation, dict):
        raise ConfigError("telegram_control.presentation must be an object.")
    _require_keys(
        presentation,
        {"status_style"},
        "telegram_control.presentation",
    )
    if presentation.get("status_style") not in {
        "compact",
        "standard",
        "detailed",
    }:
        raise ConfigError(
            "telegram_control.presentation.status_style must be compact, "
            "standard, or detailed."
        )
    return merged


def effective_settings(
    bridge_config: Optional[dict[str, Any]] = None,
    workspace_path: Optional[str] = None,
) -> dict[str, Any]:
    """Resolve validated settings using the documented precedence order."""
    config = dict(bridge_config or {})
    install_settings = config.get("telegram_control", {})
    if not isinstance(install_settings, dict):
        raise ConfigError("config.json telegram_control must be an object.")
    combined = _merge(DEFAULT_SETTINGS, install_settings)

    if workspace_path:
        workspace = Path(workspace_path).expanduser().resolve()
        if not workspace.is_dir():
            raise ConfigError(
                f"Telegram Control workspace does not exist: {workspace}"
            )
        combined = _merge(combined, _read_object(workspace / WORKSPACE_CONFIG_NAME))
        combined = _merge(
            combined,
            _read_object(workspace / LOCAL_WORKSPACE_CONFIG_NAME),
        )
    return validate_settings(combined)


def control_agent_enabled(
    bridge_config: Optional[dict[str, Any]] = None,
) -> bool:
    return bool(
        effective_settings(bridge_config)["control_agent"]["enabled"]
    )


def installed_settings(
    workspace_path: Optional[str] = None,
    *,
    config_path: Optional[Path] = None,
) -> dict[str, Any]:
    """Load the private per-install config for a worker-side rendering path."""
    path = INSTALL_CONFIG_PATH if config_path is None else Path(config_path)
    config = _read_object(path)
    existing_workspace = (
        workspace_path
        if workspace_path
        and Path(workspace_path).expanduser().is_dir()
        else None
    )
    return effective_settings(config, existing_workspace)


def confirm_topic_agent(
    bridge_config: Optional[dict[str, Any]] = None,
    workspace_path: Optional[str] = None,
) -> bool:
    return bool(
        effective_settings(bridge_config, workspace_path)["topics"][
            "confirm_agent"
        ]
    )


def _prompt_value(
    prompts: dict[str, str],
    name: str,
    workspace_path: Optional[str],
) -> str:
    inline = prompts[name].strip()
    if inline:
        return inline
    file_value = prompts[f"{name}_file"].strip()
    if not file_value:
        return ""
    candidate = Path(file_value).expanduser()
    if not candidate.is_absolute():
        if not workspace_path:
            raise ConfigError(
                f"prompts.{name}_file must be absolute outside a workspace."
            )
        workspace = Path(workspace_path).expanduser().resolve()
        candidate = (workspace / candidate).resolve()
        try:
            candidate.relative_to(workspace)
        except ValueError:
            raise ConfigError(
                f"prompts.{name}_file must stay inside the workspace."
            ) from None
    try:
        text = candidate.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"Could not read prompt file {candidate}.") from exc
    if len(text) > MAX_PROMPT_TEXT:
        raise ConfigError(
            f"Prompt file {candidate} exceeds {MAX_PROMPT_TEXT} characters."
        )
    return text


def prompt_addition(
    settings: dict[str, Any],
    workspace_path: Optional[str] = None,
) -> str:
    """Render only user-customizable guidance, never the core safety contract."""
    validated = validate_settings(settings)
    sections = []
    preamble = _prompt_value(
        validated["prompts"],
        "preamble",
        workspace_path,
    )
    response_style = _prompt_value(
        validated["prompts"],
        "response_style",
        workspace_path,
    )
    if preamble:
        sections.append(f"User-configured standing context:\n{preamble}")
    if response_style:
        sections.append(f"User-configured response style:\n{response_style}")
    return "\n\n".join(sections)

"""Resolve the effective local defaults inherited by provider adapters."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional


FALLBACK_DEFAULTS = {
    "codex": {"model": "gpt-5.6-sol", "effort": "low"},
    "claude": {"model": "sonnet", "effort": "high"},
}


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _top_level_toml_strings(path: Path) -> dict[str, str]:
    """Read the two simple top-level Codex defaults without a TOML dependency."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return {}
    values = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            break
        match = re.fullmatch(
            r"(model|model_reasoning_effort)\s*=\s*"
            r"([\"'])([A-Za-z0-9._:-]{1,100})\2\s*(?:#.*)?",
            stripped,
        )
        if match is not None:
            values[match.group(1)] = match.group(3)
    return values


def local_provider_defaults(
    provider: str,
    working_directory: Optional[str] = None,
    *,
    home_directory: Optional[Path] = None,
) -> dict[str, str]:
    """Mirror the configuration files used when an adapter omits CLI flags."""
    if provider not in FALLBACK_DEFAULTS:
        raise ValueError("Provider defaults are unavailable.")
    home = Path.home() if home_directory is None else Path(home_directory)
    resolved = dict(FALLBACK_DEFAULTS[provider])
    if provider == "codex":
        values = _top_level_toml_strings(home / ".codex" / "config.toml")
        if values.get("model"):
            resolved["model"] = values["model"]
        if values.get("model_reasoning_effort"):
            resolved["effort"] = values["model_reasoning_effort"]
        return resolved

    settings = _read_json_object(home / ".claude" / "settings.json")
    if working_directory:
        project = Path(working_directory)
        for name in ("settings.json", "settings.local.json"):
            project_settings = _read_json_object(project / ".claude" / name)
            for key in ("model", "effort"):
                value = project_settings.get(key)
                if isinstance(value, str) and value.strip():
                    settings[key] = value.strip()
    model = settings.get("model")
    effort = settings.get("effort")
    if isinstance(model, str) and model.strip():
        resolved["model"] = model.strip()
    if isinstance(effort, str) and effort.strip():
        resolved["effort"] = effort.strip()
    else:
        # Claude Code's model metadata supplies the effort when --effort is
        # omitted. Current selectable Claude families all fall back to high.
        resolved["effort"] = "high"
    return resolved


def effective_provider_config(
    provider: str,
    provider_config: Optional[dict[str, Any]] = None,
    working_directory: Optional[str] = None,
    *,
    home_directory: Optional[Path] = None,
) -> dict[str, str]:
    """Resolve explicit fields over the defaults inherited by the adapter."""
    explicit = dict(provider_config or {})
    resolved = local_provider_defaults(
        provider,
        working_directory,
        home_directory=home_directory,
    )
    for key in ("model", "effort"):
        value = explicit.get(key)
        if isinstance(value, str) and value.strip():
            resolved[key] = value.strip()
    return resolved


def describe_provider_config(
    provider: str,
    provider_config: Optional[dict[str, Any]] = None,
    working_directory: Optional[str] = None,
    *,
    home_directory: Optional[Path] = None,
) -> tuple[str, str]:
    """Return status labels that preserve and explain a Default selection."""
    explicit = dict(provider_config or {})
    if provider == "codex" and explicit.get("model_provider") == "ollama":
        model = explicit.get("model")
        model_label = (
            str(model).strip()
            if isinstance(model, str) and model.strip()
            else "Ollama default"
        )
        return model_label, "Model-controlled"
    effective = effective_provider_config(
        provider,
        explicit,
        working_directory,
        home_directory=home_directory,
    )

    def label(key: str) -> str:
        selected = explicit.get(key)
        if isinstance(selected, str) and selected.strip():
            return selected.strip()
        return f"Default (currently {effective[key]})"

    return label("model"), label("effort")


def provider_turn_summary(
    provider: str,
    provider_config: Optional[dict[str, Any]] = None,
    working_directory: Optional[str] = None,
    *,
    home_directory: Optional[Path] = None,
) -> str:
    """Return compact effective provider metadata for transient turn cards."""

    explicit = dict(provider_config or {})
    if provider == "codex" and explicit.get("model_provider") == "ollama":
        model, _effort = describe_provider_config(
            provider,
            explicit,
            working_directory,
            home_directory=home_directory,
        )
        return f"Codex · Ollama · {model}"
    effective = effective_provider_config(
        provider,
        provider_config,
        working_directory,
        home_directory=home_directory,
    )
    provider_name = "Claude" if provider == "claude" else "Codex"
    return (
        f"{provider_name} · {effective['model']} · "
        f"{effective['effort']} effort"
    )


def provider_display_name(
    provider: str,
    provider_config: Optional[dict[str, Any]] = None,
) -> str:
    """Return the human provider/backend label used by Telegram surfaces."""
    if (
        provider == "codex"
        and dict(provider_config or {}).get("model_provider") == "ollama"
    ):
        return "Codex (Ollama)"
    return "Claude" if provider == "claude" else "Codex"

"""Validated spoken-reply voices and rates for Telegram Control."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class VoiceOption:
    name: str
    label: str
    description: str


@dataclass(frozen=True)
class RateOption:
    value: str
    label: str


@dataclass(frozen=True)
class VoiceConfiguration:
    voice_name: str
    rate: str


# Spoken replies use the macOS built-in synthesizer (`say`). "system" means no
# `-v` flag, so the Mac's own default voice (System Settings > Accessibility >
# Spoken Content) is used; the others are standard bundled macOS voices.
SYSTEM_VOICE_NAME = "system"

VOICE_OPTIONS = (
    VoiceOption(SYSTEM_VOICE_NAME, "💻 Mac default", "This Mac's system voice"),
    VoiceOption("Samantha", "🇺🇸 Samantha", "American English · female"),
    VoiceOption("Reed (English (US))", "🇺🇸 Reed", "American English · male"),
    VoiceOption("Daniel", "🇬🇧 Daniel", "British English · male"),
    VoiceOption("Flo (English (UK))", "🇬🇧 Flo", "British English · female"),
    VoiceOption("Karen", "🇦🇺 Karen", "Australian English · female"),
    VoiceOption("Moira", "🇮🇪 Moira", "Irish English · female"),
    VoiceOption("Rishi", "🇮🇳 Rishi", "Indian English · male"),
)

# Rates are `say -r` words per minute.
RATE_OPTIONS = (
    RateOption("165", "Slower · 165 wpm"),
    RateOption("185", "Natural · 185 wpm"),
    RateOption("205", "Quick · 205 wpm"),
    RateOption("225", "Faster · 225 wpm"),
)

DEFAULT_VOICE_NAME = SYSTEM_VOICE_NAME
DEFAULT_RATE = "185"
DEFAULT_CONFIGURATION = VoiceConfiguration(
    voice_name=DEFAULT_VOICE_NAME,
    rate=DEFAULT_RATE,
)

VOICE_BY_NAME = {option.name: option for option in VOICE_OPTIONS}
RATE_BY_VALUE = {option.value: option for option in RATE_OPTIONS}


def validate_configuration(value: Any) -> VoiceConfiguration:
    """Normalize one stored or staged configuration against supported choices."""
    if isinstance(value, VoiceConfiguration):
        voice_name = value.voice_name
        rate = value.rate
    elif isinstance(value, dict):
        if set(value) != {"voice_name", "rate"}:
            raise ValueError("Voice configuration fields are invalid.")
        voice_name = value.get("voice_name")
        rate = value.get("rate")
    else:
        raise ValueError("Voice configuration must be an object.")
    if not isinstance(voice_name, str) or voice_name not in VOICE_BY_NAME:
        raise ValueError("Voice configuration voice is unsupported.")
    if not isinstance(rate, str) or rate not in RATE_BY_VALUE:
        raise ValueError("Voice configuration rate is unsupported.")
    return VoiceConfiguration(voice_name=voice_name, rate=rate)


def upgrade_legacy(value: Any) -> Any:
    """Map a stored configuration from the retired edge-tts voices to defaults.

    Older installs stored Microsoft neural voice names and percentage rates.
    Those are no longer valid choices, so any unknown voice or rate falls back
    to the default rather than making every spoken reply fail.
    """
    if not isinstance(value, dict) or set(value) != {"voice_name", "rate"}:
        return value
    voice_name = value.get("voice_name")
    rate = value.get("rate")
    return {
        "voice_name": (
            voice_name if voice_name in VOICE_BY_NAME else DEFAULT_VOICE_NAME
        ),
        "rate": rate if rate in RATE_BY_VALUE else DEFAULT_RATE,
    }


def as_dict(configuration: VoiceConfiguration) -> dict[str, str]:
    normalized = validate_configuration(configuration)
    return {
        "voice_name": normalized.voice_name,
        "rate": normalized.rate,
    }


def describe(configuration: VoiceConfiguration) -> tuple[str, str]:
    normalized = validate_configuration(configuration)
    return (
        VOICE_BY_NAME[normalized.voice_name].label,
        RATE_BY_VALUE[normalized.rate].label,
    )

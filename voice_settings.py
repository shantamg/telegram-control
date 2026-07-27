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


VOICE_OPTIONS = (
    VoiceOption("en-GB-SoniaNeural", "🇬🇧 Sonia", "British English · female"),
    VoiceOption("en-GB-RyanNeural", "🇬🇧 Ryan", "British English · male"),
    VoiceOption("en-US-AvaNeural", "🇺🇸 Ava", "American English · female"),
    VoiceOption("en-US-AndrewNeural", "🇺🇸 Andrew", "American English · male"),
    VoiceOption("en-US-JennyNeural", "🇺🇸 Jenny", "American English · female"),
    VoiceOption("en-US-GuyNeural", "🇺🇸 Guy", "American English · male"),
    VoiceOption("en-AU-NatashaNeural", "🇦🇺 Natasha", "Australian English · female"),
    VoiceOption(
        "en-AU-WilliamMultilingualNeural",
        "🇦🇺 William",
        "Australian English · male",
    ),
    VoiceOption("en-IN-NeerjaNeural", "🇮🇳 Neerja", "Indian English · female"),
    VoiceOption("en-IN-PrabhatNeural", "🇮🇳 Prabhat", "Indian English · male"),
)

RATE_OPTIONS = (
    RateOption("-10%", "Slower · −10%"),
    RateOption("+0%", "Natural · normal"),
    RateOption("+10%", "Quick · +10%"),
    RateOption("+20%", "Faster · +20%"),
)

DEFAULT_VOICE_NAME = "en-GB-SoniaNeural"
DEFAULT_RATE = "+10%"
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

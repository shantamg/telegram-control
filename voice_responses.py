"""Bounded, private text-to-speech generation for Telegram voice replies."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import AbstractSet, Optional

import helper_paths
import voice_settings


SPEECH_DIR = (
    Path.home()
    / "Library"
    / "Application Support"
    / "telegram-bridge"
    / "speech-outbox"
)
EDGE_TTS_BINARY = helper_paths.resolve_binary(
    "edge_tts_binary",
    Path.home() / ".local" / "bin" / "edge-tts",
    command_name="edge-tts",
)
FFMPEG_BINARY = helper_paths.resolve_binary(
    "ffmpeg_binary",
    Path("/opt/homebrew/bin/ffmpeg"),
    Path("/usr/local/bin/ffmpeg"),
    command_name="ffmpeg",
)
MAX_SPEECH_CHARACTERS = 3_500
MAX_VOICE_BYTES = 20_000_000
SYNTHESIS_TIMEOUT_SECONDS = 3 * 60
STALE_FILE_SECONDS = 24 * 60 * 60


class VoiceResponseError(RuntimeError):
    pass


def _ensure_speech_directory() -> None:
    SPEECH_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    SPEECH_DIR.chmod(0o700)


def cleanup_stale_files(
    now: Optional[float] = None,
    protected_paths: Optional[AbstractSet[str]] = None,
) -> None:
    _ensure_speech_directory()
    timestamp = time.time() if now is None else float(now)
    protected = set()
    if protected_paths is not None:
        for value in protected_paths:
            try:
                path = Path(value).resolve(strict=False)
                path.relative_to(SPEECH_DIR.resolve(strict=True))
                protected.add(path)
            except (OSError, ValueError):
                continue
    try:
        entries = list(SPEECH_DIR.iterdir())[:500]
    except OSError:
        return
    for path in entries:
        try:
            if (
                path.is_file()
                and timestamp - path.stat().st_mtime > STALE_FILE_SECONDS
                and path.resolve(strict=False) not in protected
                and (
                    path.suffix != ".ogg"
                    or protected_paths is not None
                )
            ):
                path.unlink()
        except OSError:
            continue


def speech_text(value: str) -> str:
    text = str(value)
    text = re.sub(r"```.*?```", " Code block omitted. ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", " link ", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_~>|]", " ", text)
    text = "".join(
        character
        for character in text
        if ord(character) >= 32 or character in {"\n", "\t"}
    )
    text = " ".join(text.split())
    if len(text) > MAX_SPEECH_CHARACTERS:
        suffix = "… The rest is available in the text response."
        text = text[: MAX_SPEECH_CHARACTERS - len(suffix)].rsplit(
            " ",
            1,
        )[0].rstrip()
        text += suffix
    if not text:
        raise VoiceResponseError("The response has no speakable text.")
    return text


def _safe_key(value: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_-]", "-", str(value)).strip("-")
    if not key or len(key) > 120:
        raise VoiceResponseError("The voice response key is invalid.")
    return key


def synthesize_voice(
    value: str,
    operation_key: str,
    protected_paths: Optional[AbstractSet[str]] = None,
    *,
    voice_name: str = voice_settings.DEFAULT_VOICE_NAME,
    rate: str = voice_settings.DEFAULT_RATE,
) -> Path:
    """Generate one deterministic OGG/Opus file, safe for durable retry."""
    try:
        configuration = voice_settings.validate_configuration(
            {"voice_name": voice_name, "rate": rate}
        )
    except ValueError as exc:
        raise VoiceResponseError(str(exc)) from None
    cleanup_stale_files(protected_paths=protected_paths)
    if not EDGE_TTS_BINARY.is_file() or not os.access(EDGE_TTS_BINARY, os.X_OK):
        raise VoiceResponseError("The speech synthesizer is unavailable.")
    if not FFMPEG_BINARY.is_file() or not os.access(FFMPEG_BINARY, os.X_OK):
        raise VoiceResponseError("The audio encoder is unavailable.")
    key = _safe_key(operation_key)
    final_path = SPEECH_DIR / f"{key}.ogg"
    if final_path.is_file():
        try:
            return validate_voice_path(str(final_path))
        except VoiceResponseError:
            pass
        final_path.unlink(missing_ok=True)
    nonce = secrets.token_hex(6)
    text_path = SPEECH_DIR / f".{key}-{nonce}.txt"
    media_path = SPEECH_DIR / f".{key}-{nonce}.mp3"
    encoded_path = SPEECH_DIR / f".{key}-{nonce}.ogg"
    try:
        text_path.write_text(speech_text(value), encoding="utf-8")
        text_path.chmod(0o600)
        edge = subprocess.run(
            [
                str(EDGE_TTS_BINARY),
                "--voice",
                configuration.voice_name,
                "--rate",
                configuration.rate,
                "--file",
                str(text_path),
                "--write-media",
                str(media_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=SYNTHESIS_TIMEOUT_SECONDS,
            check=False,
        )
        if edge.returncode != 0 or not media_path.is_file():
            raise VoiceResponseError("Speech synthesis failed.")
        media_path.chmod(0o600)
        ffmpeg = subprocess.run(
            [
                str(FFMPEG_BINARY),
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(media_path),
                "-c:a",
                "libopus",
                "-b:a",
                "48k",
                "-application",
                "voip",
                str(encoded_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=SYNTHESIS_TIMEOUT_SECONDS,
            check=False,
        )
        if ffmpeg.returncode != 0 or not encoded_path.is_file():
            raise VoiceResponseError("Voice-note encoding failed.")
        encoded_path.chmod(0o600)
        size = encoded_path.stat().st_size
        if size <= 0 or size > MAX_VOICE_BYTES:
            raise VoiceResponseError("The generated voice note is invalid.")
        with encoded_path.open("rb") as encoded:
            os.fsync(encoded.fileno())
        os.replace(encoded_path, final_path)
        directory_fd = os.open(str(SPEECH_DIR), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return final_path.resolve()
    except (OSError, subprocess.TimeoutExpired):
        raise VoiceResponseError("Voice generation failed.") from None
    finally:
        text_path.unlink(missing_ok=True)
        media_path.unlink(missing_ok=True)
        encoded_path.unlink(missing_ok=True)


def validate_voice_path(value: str) -> Path:
    """Validate a durable outbox file reference before upload."""
    _ensure_speech_directory()
    try:
        path = Path(value).resolve(strict=True)
        root = SPEECH_DIR.resolve(strict=True)
        path.relative_to(root)
        stat_result = path.stat()
    except (OSError, ValueError):
        raise VoiceResponseError("The queued voice file is unavailable.") from None
    if not path.is_file() or path.suffix != ".ogg":
        raise VoiceResponseError("The queued voice file is invalid.")
    if stat_result.st_uid != os.getuid() or stat_result.st_mode & 0o077:
        raise VoiceResponseError("The queued voice file permissions are unsafe.")
    if stat_result.st_size <= 0 or stat_result.st_size > MAX_VOICE_BYTES:
        raise VoiceResponseError("The queued voice file size is invalid.")
    return path


def remove_voice_file(value: str) -> None:
    try:
        validate_voice_path(value).unlink()
    except (VoiceResponseError, OSError):
        pass

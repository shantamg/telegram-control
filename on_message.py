#!/usr/bin/python3
"""Handle authorized Telegram text and voice messages."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import telegram_bridge as bridge
from durable_store import CallbackActionError, DurableStore, StoreError


HANDY_BINARY = Path("/Applications/Handy.app/Contents/MacOS/handy")
PARAKEET_MODEL_ID = "parakeet-tdt-0.6b-v3"
FFMPEG_BINARY = Path("/opt/homebrew/bin/ffmpeg")
MAX_VOICE_BYTES = 20_000_000
MAX_VOICE_SECONDS = 30 * 60
TRANSCRIPTION_TIMEOUT_SECONDS = 15 * 60
TELEGRAM_TEXT_CHUNK = 3_800
OUTPUT_SEQUENCE = 0


def deliver_api_call(
    method: str,
    params: dict,
    operation_name: str,
    route: Optional[dict] = None,
) -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    if database_path and job_id:
        with DurableStore(Path(database_path)) as store:
            store.enqueue_api_call(
                f"inbox:{job_id}:{operation_name}",
                method,
                params,
                route=route,
            )
        return

    token = bridge.read_token()
    bridge.api_call(token, method, **params)


def controller_reply_route() -> dict:
    return {
        "target_type": "controller",
        "target_id": "control",
        "policy": "reply",
        "ttl_seconds": 30 * 24 * 60 * 60,
    }


def inspect_keyboard() -> Optional[dict]:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    user_id = os.environ.get("TELEGRAM_FROM_ID")
    if not all((database_path, job_id, chat_id, user_id)):
        return None
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    thread_id = int(thread_id_text) if thread_id_text else None
    with DurableStore(Path(database_path)) as store:
        action = store.create_callback_action(
            operation_id=f"inbox:{job_id}:inspect",
            action_type="inspect_status",
            payload={"view": "transport"},
            chat_id=int(chat_id),
            message_thread_id=thread_id,
            authorized_user_id=int(user_id),
            one_time=True,
        )
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Inspect transport",
                    "callback_data": f"a:{action.token}",
                }
            ]
        ]
    }


def send_message(text: str, include_inspect_button: bool = False) -> None:
    global OUTPUT_SEQUENCE
    chat_id_text = os.environ.get("TELEGRAM_CHAT_ID")
    if chat_id_text:
        chat_id = int(chat_id_text)
    else:
        chat_id = int(bridge.load_config()["chat_id"])
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    thread_id = int(thread_id_text) if thread_id_text else None
    remaining = text.strip()
    if not remaining:
        remaining = "[empty transcript]"
    reply_markup = inspect_keyboard() if include_inspect_button else None
    while remaining:
        if len(remaining) <= TELEGRAM_TEXT_CHUNK:
            chunk, remaining = remaining, ""
        else:
            split_at = remaining.rfind("\n", 0, TELEGRAM_TEXT_CHUNK)
            if split_at < TELEGRAM_TEXT_CHUNK // 2:
                split_at = remaining.rfind(" ", 0, TELEGRAM_TEXT_CHUNK)
            if split_at < TELEGRAM_TEXT_CHUNK // 2:
                split_at = TELEGRAM_TEXT_CHUNK
            chunk = remaining[:split_at].rstrip()
            remaining = remaining[split_at:].lstrip()
        OUTPUT_SEQUENCE += 1
        params = {
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": chunk,
        }
        if not remaining and reply_markup is not None:
            params["reply_markup"] = reply_markup
        deliver_api_call(
            "sendMessage",
            params,
            f"message:{OUTPUT_SEQUENCE}",
            route=controller_reply_route(),
        )


def resolve_replied_message_route():
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    replied_message_id = os.environ.get("TELEGRAM_REPLY_TO_MESSAGE_ID")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not all((database_path, replied_message_id, chat_id)):
        return None
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    thread_id = int(thread_id_text) if thread_id_text else None
    with DurableStore(Path(database_path)) as store:
        return store.resolve_message_route(
            chat_id=int(chat_id),
            message_thread_id=thread_id,
            telegram_message_id=int(replied_message_id),
        )


def handle_callback(update: dict, callback_query: dict) -> None:
    callback_query_id = str(callback_query.get("id", ""))
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    if not database_path:
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Durable buttons require Stage 2.",
                },
                "callback-answer",
            )
        return

    chat_id = int(os.environ["TELEGRAM_CHAT_ID"])
    user_id = int(os.environ["TELEGRAM_FROM_ID"])
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    thread_id = int(thread_id_text) if thread_id_text else None
    try:
        with DurableStore(Path(database_path)) as store:
            action = store.consume_callback_action(
                callback_data=str(callback_query.get("data", "")),
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=user_id,
                update_id=int(update["update_id"]),
            )
    except CallbackActionError as exc:
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": exc.user_message,
                    "show_alert": exc.code in {"unauthorized", "invalid"},
                },
                "callback-answer",
            )
        return

    if callback_query_id:
        deliver_api_call(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": "Durable route verified.",
            },
            "callback-answer",
        )
    if action.action_type == "inspect_status":
        send_message(
            "✅ Durable button route verified.\n\n"
            "The opaque action was authorized, resolved from SQLite, and "
            "consumed exactly once."
        )
        return
    raise StoreError(f"Unsupported callback action: {action.action_type}")


def convert_to_wav(source: Path, destination: Path) -> None:
    if not FFMPEG_BINARY.is_file():
        raise bridge.BridgeError(f"ffmpeg is not installed at {FFMPEG_BINARY}.")
    result = subprocess.run(
        [
            str(FFMPEG_BINARY),
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not destination.is_file():
        raise bridge.BridgeError("ffmpeg could not decode this Telegram voice message.")


def transcribe_wav(wav_path: Path) -> str:
    if not HANDY_BINARY.is_file():
        raise bridge.BridgeError(f"Handy transcription helper is missing: {HANDY_BINARY}")
    result = subprocess.run(
        [
            str(HANDY_BINARY),
            "--transcribe-file",
            str(wav_path),
            "--model",
            PARAKEET_MODEL_ID,
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=TRANSCRIPTION_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise bridge.BridgeError("Handy's local Parakeet transcription failed.")
    try:
        payload = json.loads(result.stdout)
        return str(payload["text"]).strip()
    except (KeyError, TypeError, ValueError):
        raise bridge.BridgeError("Handy returned an unreadable transcription result.") from None


def handle_voice(voice: dict) -> None:
    file_size = int(voice.get("file_size", 0))
    duration = int(voice.get("duration", 0))
    if file_size > MAX_VOICE_BYTES:
        raise bridge.BridgeError("Voice message exceeds Telegram's 20 MB bot download limit.")
    if duration > MAX_VOICE_SECONDS:
        raise bridge.BridgeError("Voice message is longer than the configured 30-minute limit.")

    send_message("🎙️ Voice message received. Transcribing locally with Parakeet V3…")
    with tempfile.TemporaryDirectory(prefix="telegram-voice-") as temporary_directory:
        temp_dir = Path(temporary_directory)
        source_path = temp_dir / "voice.ogg"
        wav_path = temp_dir / "voice.wav"
        bridge.download_telegram_file(
            str(voice["file_id"]),
            source_path,
            max_bytes=MAX_VOICE_BYTES,
        )
        convert_to_wav(source_path, wav_path)
        transcript = transcribe_wav(wav_path)

    if transcript:
        send_message(f"📝 Transcript:\n\n{transcript}")
    else:
        send_message("📝 Parakeet did not detect any speech in that voice message.")


def main() -> int:
    update = json.load(sys.stdin)
    username = os.environ.get("TELEGRAM_FROM_USERNAME") or "unknown"

    try:
        callback_query = update.get("callback_query")
        message = update.get("message")
        if callback_query:
            handle_callback(update, callback_query)
        elif message and "voice" in message:
            print(f"Received voice message from @{username}.", flush=True)
            handle_voice(message["voice"])
        elif message and "text" in message:
            text = str(message["text"])
            print(f"Received text message from @{username}: {text}", flush=True)
            replied_message_id = os.environ.get("TELEGRAM_REPLY_TO_MESSAGE_ID")
            if replied_message_id:
                route = resolve_replied_message_route()
                if (
                    route is not None
                    and route.target_type == "controller"
                    and route.target_id == "control"
                ):
                    send_message(
                        "✅ Durable reply route verified.\n\n"
                        f"Received through the stored controller route: {text}"
                    )
                else:
                    send_message(
                        "That replied-to message has no active durable route."
                    )
            else:
                send_message(
                    f"✅ Mac script ran and received: {text}",
                    include_inspect_button=True,
                )
        else:
            send_message("Send me a text or Telegram voice message.")
        return 0
    except subprocess.TimeoutExpired:
        send_message("❌ Local transcription timed out.")
        return 1
    except (bridge.BridgeError, KeyError, ValueError) as exc:
        print(f"Voice handler error: {exc}", file=sys.stderr, flush=True)
        send_message(f"❌ {exc}")
        return 1
    except StoreError as exc:
        print(f"Durable handler error: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/python3
"""A small, dependency-free Telegram bridge for macOS."""

from __future__ import annotations

import argparse
import getpass
import http.client
import json
import os
import plistlib
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

import voice_responses


APP_NAME = "telegram-bridge"
KEYCHAIN_TOKEN_SERVICE = "telegram-bridge-bot-token"
LAUNCH_AGENT_LABEL = "local.telegram-bridge"
CONFIG_DIR = Path.home() / "Library" / "Application Support" / APP_NAME
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "offset"
LOG_DIR = Path.home() / "Library" / "Logs"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
SCRIPT_PATH = Path(__file__).resolve()
DEFAULT_HANDLER_PATH = SCRIPT_PATH.with_name("on_message.py")
# Telegram will not let a bot promote itself, but it will grant these rights in
# the same tap that adds the bot, if the invite link asks for them: reading
# ordinary group messages at all requires admin status, changing the group icon
# requires change_info, retiring progress cards requires delete_messages, and
# managed/worker topics require manage_topics.
GROUP_ADMIN_RIGHTS = ("change_info", "delete_messages", "manage_topics")
RETRYABLE_HANDLER_EXIT = 75


class BridgeError(RuntimeError):
    pass


class RetryableHandlerError(BridgeError):
    """A handler failure that the durable inbox should retry silently."""


_RETRYABLE_TELEGRAM_ERRORS = (
    "Could not reach Telegram.",
    "Could not download the Telegram file.",
    "Could not start the Telegram API helper.",
    "Telegram call exceeded the total deadline.",
    "The Telegram API helper exited unexpectedly.",
)


def is_retryable_telegram_error(error: Any) -> bool:
    """Recognize transport failures that should use the durable retry path."""

    text = str(error)
    return any(marker in text for marker in _RETRYABLE_TELEGRAM_ERRORS)


def clean_and_validate_token(raw_token: str) -> str:
    # Some terminals can include bracketed-paste or keyboard escape sequences
    # in hidden input. Remove complete ANSI escape sequences before validating.
    token = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|.)", "", raw_token).strip()
    if not re.fullmatch(r"\d{6,15}:[A-Za-z0-9_-]{30,100}", token):
        raise BridgeError(
            "The pasted value is not a valid Telegram bot token. "
            "Copy the replacement token directly from BotFather and try again."
        )
    return token


def keychain_account() -> str:
    return getpass.getuser()


def read_token() -> str:
    result = subprocess.run(
        [
            "/usr/bin/security",
            "find-generic-password",
            "-a",
            keychain_account(),
            "-s",
            KEYCHAIN_TOKEN_SERVICE,
            "-w",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BridgeError("Telegram bot token is not in Keychain. Run the setup command first.")
    token = result.stdout.strip()
    if not token:
        raise BridgeError("The Telegram bot token stored in Keychain is empty.")
    return token


def store_token(token: str) -> None:
    # The token is never written to disk or printed. macOS's security tool requires
    # the secret as an argument when updating a generic-password item.
    result = subprocess.run(
        [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            keychain_account(),
            "-s",
            KEYCHAIN_TOKEN_SERVICE,
            "-w",
            token,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise BridgeError("Could not save the Telegram bot token in macOS Keychain.")


API_BASE_URL = "https://api.telegram.org"
API_SOCKET_TIMEOUT_SECONDS = 70.0
API_TOTAL_DEADLINE_SECONDS = 180.0
API_MAX_RESPONSE_BYTES = 8_000_000
MAX_CHAT_PHOTO_BYTES = 10_000_000


def _read_capped(response: Any, max_bytes: Optional[int] = None) -> bytes:
    if max_bytes is None:
        max_bytes = API_MAX_RESPONSE_BYTES
    chunks = []
    received = 0
    while True:
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        received += len(chunk)
        if received > max_bytes:
            raise BridgeError("Telegram response exceeded the size limit.")
        chunks.append(chunk)
    return b"".join(chunks)


def perform_api_call(
    token: str,
    method: str,
    params: dict[str, Any],
    base_url: str = API_BASE_URL,
) -> Any:
    """Execute one Telegram Bot API request in the current process.

    Runs inside the killable `api-exec` helper child in production, so it
    needs only per-socket timeouts; the parent enforces the hard total
    deadline by killing the child. Standard urllib handling is retained, so
    system HTTPS-proxy configuration keeps working.
    """
    request_params = dict(params)
    voice_file_path = request_params.pop("__voice_file_path", None)
    photo_file_path = request_params.pop("__photo_file_path", None)
    if voice_file_path is not None and photo_file_path is not None:
        raise BridgeError("A Telegram request can upload only one local file.")
    encoded_params: dict[str, str] = {}
    for key, value in request_params.items():
        if value is None:
            continue
        encoded_params[key] = json.dumps(value) if isinstance(value, (list, dict)) else str(value)
    headers: dict[str, str] = {}
    if voice_file_path is None and photo_file_path is None:
        request_data = urllib.parse.urlencode(encoded_params).encode("utf-8")
    else:
        if voice_file_path is not None:
            if method != "sendVoice":
                raise BridgeError("A voice file can only be used with sendVoice.")
            try:
                upload_path = voice_responses.validate_voice_path(
                    str(voice_file_path)
                )
                upload_bytes = upload_path.read_bytes()
            except OSError:
                raise BridgeError("The queued voice file is unavailable.") from None
            except voice_responses.VoiceResponseError as exc:
                raise BridgeError(str(exc)) from None
            upload_field = "voice"
            upload_filename = "voice.ogg"
            upload_content_type = "audio/ogg"
        else:
            if method != "setChatPhoto":
                raise BridgeError(
                    "A chat photo file can only be used with setChatPhoto."
                )
            try:
                upload_path = Path(str(photo_file_path)).expanduser().resolve(
                    strict=True
                )
                if not upload_path.is_file():
                    raise OSError
                photo_size = upload_path.stat().st_size
                if photo_size <= 0 or photo_size > MAX_CHAT_PHOTO_BYTES:
                    raise BridgeError(
                        "The chat photo must be between 1 byte and 10 MB."
                    )
                upload_bytes = upload_path.read_bytes()
            except BridgeError:
                raise
            except OSError:
                raise BridgeError("The chat photo file is unavailable.") from None
            if upload_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                upload_content_type = "image/png"
                upload_filename = "chat-photo.png"
            elif upload_bytes.startswith(b"\xff\xd8"):
                upload_content_type = "image/jpeg"
                upload_filename = "chat-photo.jpg"
            else:
                raise BridgeError("The chat photo must be a PNG or JPEG image.")
            upload_field = "photo"
        boundary = f"telegram-control-{uuid.uuid4().hex}"
        body = bytearray()
        for key, value in encoded_params.items():
            body.extend(f"--{boundary}\r\n".encode("ascii"))
            body.extend(
                (
                    f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                ).encode("utf-8")
            )
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{upload_field}"; '
                f'filename="{upload_filename}"\r\n'
            ).encode("ascii")
        )
        body.extend(f"Content-Type: {upload_content_type}\r\n\r\n".encode("ascii"))
        body.extend(upload_bytes)
        body.extend(b"\r\n")
        body.extend(f"--{boundary}--\r\n".encode("ascii"))
        request_data = bytes(body)
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    request = urllib.request.Request(
        f"{base_url}/bot{token}/{method}",
        data=request_data,
        headers=headers,
        method="POST",
    )
    error_status: Optional[int] = None
    try:
        try:
            with urllib.request.urlopen(
                request,
                timeout=API_SOCKET_TIMEOUT_SECONDS,
            ) as response:
                raw_body = _read_capped(response)
        except urllib.error.HTTPError as exc:
            error_status = int(exc.code)
            try:
                raw_body = _read_capped(exc)
            except BridgeError:
                raw_body = b""
            finally:
                exc.close()
    except BridgeError:
        raise
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,
    ):
        raise BridgeError(
            "Could not reach Telegram. Check this Mac's internet connection."
        ) from None

    def _redacted(description: str) -> str:
        # A remotely supplied description could reflect the request path;
        # never let the token travel into logs or durable error records.
        return description.replace(token, "[redacted]")

    if error_status is not None:
        description = "Telegram rejected the request."
        try:
            parsed: Any = json.loads(raw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("description"), str):
            description = _redacted(parsed["description"])
        raise BridgeError(description)
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise BridgeError("Telegram returned an unreadable response.") from None
    if not isinstance(payload, dict) or "result" not in payload:
        raise BridgeError("Telegram returned an unreadable response.")
    if payload.get("ok") is not True:
        description = payload.get("description")
        raise BridgeError(
            _redacted(description)
            if isinstance(description, str) and description
            else "Telegram API request failed."
        )
    return payload["result"]


def api_exec_command(_: argparse.Namespace) -> None:
    """Helper-child entrypoint: one API request described on stdin.

    The bot token arrives on stdin — never in process arguments — and the
    outcome leaves as one JSON object on stdout. Telegram-level failures are
    reported as `ok: false`; a non-zero exit means the helper itself broke.

    The parent's kill-on-timeout is the primary bound; a daemon watchdog
    thread additionally makes the helper self-terminating, exiting hard at
    its own deadline (with a small margin) or as soon as its parent dies.
    An orphaned helper therefore can never keep a request in flight after
    the supervisor tears a worker down. The deadline uses wall-clock time
    deliberately: monotonic time pauses across macOS sleep while durable
    outbox leases do not, so a helper that slept through its deadline must
    exit immediately on wake rather than resume a stale request.
    """
    payload = json.load(sys.stdin)
    deadline_seconds = float(
        payload.get("total_deadline_seconds") or API_TOTAL_DEADLINE_SECONDS
    )
    # The expected parent PID comes from the payload: sampling getppid()
    # here would race a parent that already died, leaving the baseline at
    # launchd and orphan detection blind.
    parent_pid = int(payload.get("parent_pid") or os.getppid())

    def _self_watchdog() -> None:
        deadline_at = time.time() + deadline_seconds + 5.0
        while True:
            if time.time() >= deadline_at:
                os._exit(70)
            if os.getppid() != parent_pid:
                os._exit(71)
            time.sleep(0.5)

    threading.Thread(target=_self_watchdog, daemon=True).start()
    try:
        result = perform_api_call(
            str(payload["token"]),
            str(payload["method"]),
            dict(payload.get("params") or {}),
            base_url=str(payload.get("base_url") or API_BASE_URL),
        )
        outcome: dict[str, Any] = {"ok": True, "result": result}
    except BridgeError as exc:
        outcome = {"ok": False, "error": str(exc)}
    print(json.dumps(outcome, separators=(",", ":")))


def api_call(
    token: str,
    method: str,
    total_deadline_seconds: float = API_TOTAL_DEADLINE_SECONDS,
    delivery_lock_fd: Optional[int] = None,
    **params: Any,
) -> Any:
    """Call the Telegram Bot API with a hard total wall-clock deadline.

    The request runs in a killable helper subprocess; on deadline expiry the
    child is killed. Killing a process bounds every phase of the operation —
    including macOS `getaddrinfo`, which no in-process signal or socket
    teardown can reliably interrupt. An expiry after Telegram accepted the
    request is an ambiguous outcome and follows the documented at-least-once
    retry semantics.

    When `delivery_lock_fd` is given (the outbox sender's already-locked
    delivery-lock descriptor — a reserved parameter, never a Telegram
    field), the helper inherits that exact open file description, so the
    kernel keeps the BSD flock held until the helper itself exits even if
    this parent process is SIGKILLed mid-call. Inherited lock ownership is
    the delivery-ordering fence; the helper's self-termination is cleanup.
    """
    request_payload = json.dumps(
        {
            "token": token,
            "method": method,
            "params": {
                key: value
                for key, value in params.items()
                if value is not None
            },
            "base_url": API_BASE_URL,
            "total_deadline_seconds": float(total_deadline_seconds),
            "parent_pid": os.getpid(),
        },
        separators=(",", ":"),
    )
    try:
        child = subprocess.Popen(
            [sys.executable, str(SCRIPT_PATH), "api-exec"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(
                (int(delivery_lock_fd),)
                if delivery_lock_fd is not None
                else ()
            ),
        )
    except (OSError, ValueError):
        raise BridgeError("Could not start the Telegram API helper.") from None
    try:
        stdout, _stderr = child.communicate(
            input=request_payload,
            timeout=float(total_deadline_seconds),
        )
    except subprocess.TimeoutExpired:
        child.kill()
        try:
            child.communicate(timeout=30)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise BridgeError("Telegram call exceeded the total deadline.") from None
    except BaseException:
        # KeyboardInterrupt or any other failure while the parent survives:
        # never leave the helper or its pipes behind.
        child.kill()
        try:
            child.communicate(timeout=30)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        raise
    if child.returncode != 0:
        raise BridgeError("The Telegram API helper exited unexpectedly.")
    try:
        outcome = json.loads(stdout)
    except ValueError:
        raise BridgeError(
            "The Telegram API helper returned an unreadable result."
        ) from None
    if not isinstance(outcome, dict):
        raise BridgeError("The Telegram API helper returned an unreadable result.")
    if outcome.get("ok") is not True:
        error = outcome.get("error")
        raise BridgeError(
            error
            if isinstance(error, str) and error
            else "Telegram API request failed."
        )
    if "result" not in outcome:
        raise BridgeError("The Telegram API helper returned an unreadable result.")
    return outcome["result"]


def download_telegram_file(file_id: str, destination: Path, max_bytes: int = 20_000_000) -> None:
    """Download a Telegram file without the bot token in process arguments.

    The `getFile` lookup goes through the deadline-bounded API helper, which
    receives the token on stdin; the content download itself runs in this
    process.
    """
    token = read_token()
    file_info = api_call(token, "getFile", file_id=file_id)
    file_size = int(file_info.get("file_size", 0))
    if file_size > max_bytes:
        raise BridgeError(
            f"Telegram file is {file_size / 1_000_000:.1f} MB; "
            f"the configured limit is {max_bytes / 1_000_000:.0f} MB."
        )

    file_path = file_info.get("file_path")
    if not file_path:
        raise BridgeError("Telegram did not provide a download path for this file.")
    quoted_path = urllib.parse.quote(str(file_path), safe="/")
    request = urllib.request.Request(
        f"https://api.telegram.org/file/bot{token}/{quoted_path}",
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=70) as response:
            content_length = int(response.headers.get("Content-Length", 0))
            if content_length > max_bytes:
                raise BridgeError(
                    f"Telegram file is {content_length / 1_000_000:.1f} MB; "
                    f"the configured limit is {max_bytes / 1_000_000:.0f} MB."
                )
            downloaded = 0
            with destination.open("wb") as output_file:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        raise BridgeError("Telegram file exceeded the configured download limit.")
                    output_file.write(chunk)
    except BridgeError:
        raise
    except urllib.error.HTTPError:
        raise BridgeError("Telegram rejected the file download request.") from None
    except (urllib.error.URLError, http.client.InvalidURL):
        raise BridgeError("Could not download the Telegram file.") from None
    except OSError:
        raise BridgeError("Could not save the Telegram file temporarily.") from None


def ensure_private_file(path: Path) -> None:
    path.chmod(0o600)


def load_config() -> dict[str, Any]:
    try:
        with CONFIG_PATH.open(encoding="utf-8") as config_file:
            return json.load(config_file)
    except FileNotFoundError:
        raise BridgeError("Telegram bridge is not paired. Run the setup command first.") from None
    except (OSError, ValueError):
        raise BridgeError(f"Could not read configuration at {CONFIG_PATH}.") from None


def group_setup_link(config: Optional[dict[str, Any]] = None) -> str:
    """Build the deep link that adds this bot to a group as an administrator.

    A bot cannot create a group or promote itself, but Telegram's startgroup
    link collapses adding it and granting its rights into one confirmation, so
    the owner is never asked to promote it as a separate step.
    """
    resolved = config if config is not None else load_config()
    username = str(resolved.get("bot_username", "")).strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", username):
        raise BridgeError(
            "The paired bot username is unavailable. Run setup again."
        )
    rights = "+".join(GROUP_ADMIN_RIGHTS)
    return f"https://t.me/{username}?startgroup=true&admin={rights}"


def save_config(config: dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = CONFIG_PATH.with_suffix(".tmp")
    with temporary_path.open("w", encoding="utf-8") as config_file:
        json.dump(config, config_file, indent=2)
        config_file.write("\n")
    ensure_private_file(temporary_path)
    temporary_path.replace(CONFIG_PATH)
    ensure_private_file(CONFIG_PATH)


def read_offset() -> Optional[int]:
    try:
        return int(STATE_PATH.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None


def save_offset(offset: int) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary_path = STATE_PATH.with_suffix(".tmp")
    temporary_path.write_text(f"{offset}\n", encoding="utf-8")
    ensure_private_file(temporary_path)
    temporary_path.replace(STATE_PATH)
    ensure_private_file(STATE_PATH)


def describe_message(message: dict[str, Any]) -> str:
    sender = message.get("from", {})
    name = " ".join(
        part for part in (sender.get("first_name", ""), sender.get("last_name", "")) if part
    )
    username = sender.get("username")
    identity = name or "Unknown user"
    if username:
        identity += f" (@{username})"
    return f"{identity}, chat ID {message.get('chat', {}).get('id')}"


def setup_command(_: argparse.Namespace) -> None:
    print("Create a bot in Telegram first:")
    print("  1. Open https://t.me/BotFather")
    print("  2. Send /newbot and follow the prompts")
    print("  3. Copy the token BotFather gives you")
    print()

    token = clean_and_validate_token(
        getpass.getpass("Paste the replacement bot token (input is hidden): ")
    )
    if not token:
        raise BridgeError("No token entered.")

    bot = api_call(token, "getMe")
    bot_username = bot.get("username", "")
    print(f"Token verified for @{bot_username}.")
    store_token(token)

    # Long polling and webhooks are mutually exclusive.
    api_call(token, "deleteWebhook", drop_pending_updates=False)
    print(f"Open https://t.me/{bot_username}, tap Start, and send the bot: pair")
    input("Press Return here after sending it... ")

    print("Waiting for your private message...")
    deadline = time.monotonic() + 120
    detected: list[dict[str, Any]] = []
    highest_update_id: Optional[int] = None
    while time.monotonic() < deadline and not detected:
        updates = api_call(
            token,
            "getUpdates",
            timeout=20,
            allowed_updates=["message"],
        )
        for update in updates:
            highest_update_id = max(highest_update_id or 0, int(update["update_id"]))
            message = update.get("message")
            if message and message.get("chat", {}).get("type") == "private":
                detected.append(message)

    if not detected:
        raise BridgeError("No private message arrived within two minutes. Run setup again.")

    unique_chats: dict[int, dict[str, Any]] = {}
    for message in detected:
        unique_chats[int(message["chat"]["id"])] = message
    choices = list(unique_chats.values())

    if len(choices) == 1:
        selected = choices[0]
        answer = input(f"Pair with {describe_message(selected)}? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            raise BridgeError("Pairing cancelled.")
    else:
        print("Choose the private chat to authorize:")
        for index, message in enumerate(choices, start=1):
            print(f"  {index}. {describe_message(message)}")
        try:
            selected = choices[int(input("Number: ").strip()) - 1]
        except (ValueError, IndexError):
            raise BridgeError("Invalid selection; pairing cancelled.") from None

    config = {
        "chat_id": int(selected["chat"]["id"]),
        "owner_user_id": int(selected["from"]["id"]),
        "bot_username": bot_username,
        "handler_path": str(DEFAULT_HANDLER_PATH),
    }
    save_config(config)
    if highest_update_id is not None:
        save_offset(highest_update_id + 1)

    print("Paired successfully.")
    print(f"Test computer → phone: {SCRIPT_PATH} send 'Hello from my Mac'")
    print(f"Test phone → computer: {SCRIPT_PATH} listen")
    print(f"Install background listener: {SCRIPT_PATH} install")


def send_command(args: argparse.Namespace) -> None:
    config = load_config()
    token = read_token()
    if args.text:
        text = " ".join(args.text)
    elif not sys.stdin.isatty():
        text = sys.stdin.read()
    else:
        raise BridgeError("Provide message text as arguments or pipe it on standard input.")
    text = text.strip()
    if not text:
        raise BridgeError("Message text is empty.")
    api_call(token, "sendMessage", chat_id=config["chat_id"], text=text)
    print("Message sent.")


def handler_command(handler_path: Path) -> list[str]:
    if handler_path.suffix == ".py":
        return [sys.executable, str(handler_path)]
    if not os.access(handler_path, os.X_OK):
        raise BridgeError(f"Handler is not executable: {handler_path}")
    return [str(handler_path)]


def explicit_reply_message_id(message: dict[str, Any]) -> str:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return ""
    if (
        message.get("is_topic_message") is True
        and reply.get("forum_topic_created") is not None
        and reply.get("message_thread_id") == message.get("message_thread_id")
    ):
        # Telegram automatically represents an ordinary top-level topic
        # message as a reply to the topic-creation service message. It is not a
        # user-selected reply and therefore must not enter return-route lookup.
        return ""
    message_id = reply.get("message_id")
    return str(message_id) if message_id is not None else ""


def update_authorization_failure(
    config: dict[str, Any],
    update: dict[str, Any],
) -> Optional[str]:
    """Return the failed authorization dimension, or None when allowed.

    Besides the paired private chat, only owner-authored messages in a private
    forum topic are admitted. Durable forum enrollment is enforced by the
    handler before such a topic may reach Control.
    """
    callback_query = update.get("callback_query")
    message = update.get("message")
    if not message and isinstance(callback_query, dict):
        message = callback_query.get("message")
    if not isinstance(message, dict):
        return "chat"

    chat = message.get("chat") or {}
    owner_user_id = int(config.get("owner_user_id") or config["chat_id"])
    chat_type = str(chat.get("type", ""))
    chat_id = int(chat.get("id", 0))
    authorized_private_chat = (
        chat_type == "private" and chat_id == int(config["chat_id"])
    )
    authorized_private_forum = (
        chat_type == "supergroup"
        and chat.get("is_forum") is True
        and not chat.get("username")
        and message.get("message_thread_id") is not None
    )
    if not (authorized_private_chat or authorized_private_forum):
        return "chat"

    sender = (
        callback_query.get("from", {})
        if isinstance(callback_query, dict)
        else message.get("from", {})
    )
    try:
        sender_id = int(sender.get("id", 0))
    except (TypeError, ValueError):
        return "user"
    return None if sender_id == owner_user_id else "user"


def process_update(
    config: dict[str, Any],
    update: dict[str, Any],
    extra_environment: Optional[dict[str, str]] = None,
) -> None:
    callback_query = update.get("callback_query")
    message = update.get("message")
    if not message and isinstance(callback_query, dict):
        message = callback_query.get("message")
    if not message:
        return

    authorization_failure = update_authorization_failure(config, update)
    if authorization_failure is not None:
        print(
            f"Ignored an unauthorized {authorization_failure} "
            f"(update {update['update_id']}).",
            flush=True,
        )
        return

    chat = message.get("chat", {})
    chat_type = str(chat.get("type", ""))

    handler_path = Path(config["handler_path"]).expanduser().resolve()
    if not handler_path.is_file():
        raise BridgeError(f"Handler does not exist: {handler_path}")

    sender = (
        callback_query.get("from", {})
        if isinstance(callback_query, dict)
        else message.get("from", {})
    )
    environment = os.environ.copy()
    topic_service_message = message.get("reply_to_message") or {}
    topic_created = message.get("forum_topic_created")
    if not isinstance(topic_created, dict):
        topic_created = (
            topic_service_message.get("forum_topic_created")
            if isinstance(topic_service_message, dict)
            else None
        )
    topic_name = (
        str(topic_created.get("name", ""))
        if isinstance(topic_created, dict)
        else ""
    )
    environment.update(
        {
            "TELEGRAM_CHAT_ID": str(chat["id"]),
            "TELEGRAM_CHAT_TYPE": chat_type,
            "TELEGRAM_CHAT_TITLE": str(chat.get("title", "")),
            "TELEGRAM_TOPIC_NAME": topic_name,
            "TELEGRAM_MESSAGE_ID": str(message.get("message_id", "")),
            "TELEGRAM_MESSAGE_THREAD_ID": str(message.get("message_thread_id", "")),
            "TELEGRAM_REPLY_TO_MESSAGE_ID": explicit_reply_message_id(message),
            "TELEGRAM_TEXT": str(message.get("text", "")),
            "TELEGRAM_FROM_ID": str(sender.get("id", "")),
            "TELEGRAM_FROM_USERNAME": str(sender.get("username", "")),
            # The handler names the bot in user-facing copy and in the
            # add-to-group link, so it gets the paired identity from the same
            # config the worker already loaded rather than re-reading it.
            "TELEGRAM_BOT_USERNAME": str(config.get("bot_username", "")),
            "TELEGRAM_CALLBACK_QUERY_ID": str(
                callback_query.get("id", "") if isinstance(callback_query, dict) else ""
            ),
        }
    )
    if extra_environment:
        environment.update(extra_environment)
    result = subprocess.run(
        handler_command(handler_path),
        input=json.dumps(update),
        text=True,
        env=environment,
    )
    if result.returncode != 0:
        if result.returncode == RETRYABLE_HANDLER_EXIT:
            raise RetryableHandlerError(
                "Handler reported a retryable Telegram transport failure."
            )
        raise BridgeError(f"Handler exited with status {result.returncode}.")


def listen_command(_: argparse.Namespace) -> None:
    config = load_config()
    token = read_token()
    offset = read_offset()
    print(
        f"Listening as @{config.get('bot_username', 'unknown')} "
        f"for paired chat {config['chat_id']}.",
        flush=True,
    )

    while True:
        try:
            updates = api_call(
                token,
                "getUpdates",
                offset=offset,
                timeout=50,
                allowed_updates=["message"],
            )
            for update in updates:
                next_offset = int(update["update_id"]) + 1
                try:
                    process_update(config, update)
                except BridgeError as exc:
                    print(f"Update {update['update_id']}: {exc}", file=sys.stderr, flush=True)
                finally:
                    offset = next_offset
                    save_offset(offset)
        except BridgeError as exc:
            print(f"{exc} Retrying in 5 seconds.", file=sys.stderr, flush=True)
            time.sleep(5)


def launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["/bin/launchctl", *arguments],
        capture_output=True,
        text=True,
        check=check,
    )


def install_command(args: argparse.Namespace) -> None:
    config = load_config()
    read_token()

    if args.handler:
        handler_path = Path(args.handler).expanduser().resolve()
        if not handler_path.is_file():
            raise BridgeError(f"Handler does not exist: {handler_path}")
        handler_command(handler_path)
        config["handler_path"] = str(handler_path)
        save_config(config)

    handler_path = Path(config["handler_path"]).expanduser().resolve()
    if not handler_path.is_file():
        raise BridgeError(f"Handler does not exist: {handler_path}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [sys.executable, str(SCRIPT_PATH), "listen"],
        "WorkingDirectory": str(SCRIPT_PATH.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(LOG_DIR / f"{APP_NAME}.log"),
        "StandardErrorPath": str(LOG_DIR / f"{APP_NAME}.error.log"),
    }
    with PLIST_PATH.open("wb") as plist_file:
        plistlib.dump(plist, plist_file)
    ensure_private_file(PLIST_PATH)

    domain = f"gui/{os.getuid()}"
    launchctl("bootout", domain, str(PLIST_PATH), check=False)
    result = launchctl("bootstrap", domain, str(PLIST_PATH), check=False)
    if result.returncode != 0:
        raise BridgeError(result.stderr.strip() or "launchctl could not install the listener.")
    launchctl("kickstart", "-k", f"{domain}/{LAUNCH_AGENT_LABEL}", check=False)
    print("Background listener installed and started.")
    print(f"Handler: {config['handler_path']}")
    print(f"Logs: {LOG_DIR / f'{APP_NAME}.log'}")
    print(f"Errors: {LOG_DIR / f'{APP_NAME}.error.log'}")


def uninstall_command(_: argparse.Namespace) -> None:
    domain = f"gui/{os.getuid()}"
    launchctl("bootout", domain, str(PLIST_PATH), check=False)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
    print("Background listener removed. Pairing and the Keychain token were kept.")


def status_command(_: argparse.Namespace) -> None:
    try:
        config = load_config()
        paired = f"yes (chat {config['chat_id']}, @{config.get('bot_username', 'unknown')})"
        handler = config.get("handler_path", "not configured")
    except BridgeError:
        paired = "no"
        handler = "not configured"
    try:
        read_token()
        token_present = "yes"
    except BridgeError:
        token_present = "no"

    domain = f"gui/{os.getuid()}"
    result = launchctl("print", f"{domain}/{LAUNCH_AGENT_LABEL}", check=False)
    listener = "running/loaded" if result.returncode == 0 else "not loaded"
    print(f"Paired: {paired}")
    print(f"Token in Keychain: {token_present}")
    print(f"Listener: {listener}")
    print(f"Handler: {handler}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send and receive Telegram messages from this Mac."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser("setup", help="Store the bot token and pair your chat.")
    setup_parser.set_defaults(function=setup_command)

    send_parser = subparsers.add_parser("send", help="Send a Telegram message to your phone.")
    send_parser.add_argument("text", nargs="*", help="Text to send; stdin is used when omitted.")
    send_parser.set_defaults(function=send_command)

    listen_parser = subparsers.add_parser("listen", help="Listen in the foreground.")
    listen_parser.set_defaults(function=listen_command)

    install_parser = subparsers.add_parser(
        "install", help="Install and start the macOS background listener."
    )
    install_parser.add_argument(
        "--handler",
        help="Executable or Python script to run for each authorized incoming message.",
    )
    install_parser.set_defaults(function=install_command)

    uninstall_parser = subparsers.add_parser(
        "uninstall", help="Remove the macOS background listener."
    )
    uninstall_parser.set_defaults(function=uninstall_command)

    status_parser = subparsers.add_parser("status", help="Show bridge status without secrets.")
    status_parser.set_defaults(function=status_command)

    api_exec_parser = subparsers.add_parser(
        "api-exec",
        help=argparse.SUPPRESS,
    )
    api_exec_parser.set_defaults(function=api_exec_command)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.function(args)
        return 0
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except BridgeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

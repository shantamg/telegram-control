#!/usr/bin/python3
"""Send a scoped Telegram update from the current managed agent turn."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import telegram_bridge
import voice_responses
from durable_store import DurableStore, StoreError


MAX_INPUT_CHARACTERS = 3_500
KEY_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise StoreError(
            "This command is only available inside an active Telegram-managed turn."
        )
    return value


def notification_operation_id(
    mailbox_id: int,
    mode: str,
    key: str,
    text: str,
) -> str:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return (
        f"agent-mailbox:{int(mailbox_id)}:notification:"
        f"{mode}:{key}:{digest}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Use scoped Telegram capabilities from this managed turn."
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("text", "voice"):
        update_parser = subparsers.add_parser(mode)
        update_parser.add_argument(
            "--key",
            required=True,
            help="Stable lowercase idempotency key, such as milestone or completion.",
        )
    icon_parser = subparsers.add_parser("group-icon")
    icon_parser.add_argument(
        "--image",
        type=Path,
        required=True,
        help="Absolute path to a local PNG or JPEG image.",
    )
    subparsers.add_parser(
        "topic-teardown",
        help="Post a confirmation card for tearing down the current topic.",
    )
    return parser.parse_args()


def set_current_group_icon(
    *,
    store: DurableStore,
    agent_id: str,
    mailbox_id: int,
    worker_id: str,
    image_path: Path,
) -> None:
    target = store.agent_notification_target(
        agent_id=agent_id,
        mailbox_id=mailbox_id,
        worker_id=worker_id,
    )
    if target.chat_id >= 0:
        raise StoreError(
            "A group icon can only be changed from a Telegram group or supergroup."
        )
    resolved_image = image_path.expanduser().resolve(strict=True)
    if not resolved_image.is_file():
        raise StoreError("The group icon path is not a file.")

    token = telegram_bridge.read_token()
    bot = telegram_bridge.api_call(token, "getMe")
    member = telegram_bridge.api_call(
        token,
        "getChatMember",
        chat_id=target.chat_id,
        user_id=int(bot["id"]),
    )
    status = str(member.get("status", ""))
    if status not in {"administrator", "creator"} or (
        status == "administrator" and member.get("can_change_info") is not True
    ):
        raise StoreError(
            "The Telegram bot must be a group administrator with "
            "Change group info permission."
        )

    # Revalidate the live turn immediately before the external mutation.
    current_target = store.agent_notification_target(
        agent_id=agent_id,
        mailbox_id=mailbox_id,
        worker_id=worker_id,
    )
    if current_target.chat_id != target.chat_id:
        raise StoreError("The Telegram group changed while preparing the icon.")
    telegram_bridge.api_call(
        token,
        "setChatPhoto",
        chat_id=target.chat_id,
        __photo_file_path=str(resolved_image),
    )


def main() -> int:
    args = parse_args()
    database_path = Path(required_environment("TELEGRAM_CONTROL_DB"))
    agent_id = required_environment("TELEGRAM_CONTROL_AGENT_ID")
    mailbox_id = int(required_environment("TELEGRAM_CONTROL_MAILBOX_ID"))
    worker_id = required_environment("TELEGRAM_CONTROL_WORKER_ID")
    if args.mode == "group-icon":
        with DurableStore(database_path) as store:
            set_current_group_icon(
                store=store,
                agent_id=agent_id,
                mailbox_id=mailbox_id,
                worker_id=worker_id,
                image_path=args.image,
            )
        print("Telegram group icon updated.")
        return 0
    if args.mode == "topic-teardown":
        with DurableStore(database_path) as store:
            store.enqueue_agent_topic_teardown_prompt(
                agent_id=agent_id,
                mailbox_id=mailbox_id,
                worker_id=worker_id,
            )
        print("Telegram topic teardown confirmation queued.")
        return 0

    if not KEY_PATTERN.fullmatch(args.key) or len(args.key) > 40:
        raise StoreError(
            "The update key must be 1 to 40 lowercase letters, numbers, or hyphens."
        )
    text = sys.stdin.read(MAX_INPUT_CHARACTERS + 1).strip()
    if not text or len(text) > MAX_INPUT_CHARACTERS:
        raise StoreError(
            "The update must contain 1 to 3,500 characters on standard input."
        )
    operation_id = notification_operation_id(
        mailbox_id,
        args.mode,
        args.key,
        text,
    )
    with DurableStore(database_path) as store:
        existing_state = store.outbox_operation_state(operation_id)
        if existing_state is not None:
            print(f"Telegram update already {existing_state}.")
            return 0
        voice_path: Path | None = None
        if args.mode == "voice":
            voice_path = voice_responses.synthesize_voice(
                text,
                f"mailbox-{mailbox_id}-{args.key}-"
                f"{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}",
                protected_paths=store.pending_voice_file_paths(),
            )
        try:
            store.enqueue_agent_notification(
                operation_id=operation_id,
                agent_id=agent_id,
                mailbox_id=mailbox_id,
                worker_id=worker_id,
                text=text,
                voice_file_path=str(voice_path) if voice_path is not None else None,
            )
        except BaseException:
            if voice_path is not None:
                voice_path.unlink(missing_ok=True)
            raise
    print(f"Telegram {args.mode} update queued.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        OSError,
        ValueError,
        StoreError,
        telegram_bridge.BridgeError,
        voice_responses.VoiceResponseError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

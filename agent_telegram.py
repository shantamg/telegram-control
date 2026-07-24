#!/usr/bin/python3
"""Request a scoped Telegram action from the current managed agent turn."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

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
        description="Request a durable Telegram action from this managed turn."
    )
    parser.add_argument("mode", choices=("text", "voice", "controls"))
    parser.add_argument(
        "--key",
        help="Stable lowercase idempotency key, such as milestone or completion.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.mode in {"text", "voice"} and (
        args.key is None
        or not KEY_PATTERN.fullmatch(args.key)
        or len(args.key) > 40
    ):
        raise StoreError(
            "The update key must be 1 to 40 lowercase letters, numbers, or hyphens."
        )
    if args.mode == "controls" and args.key is not None:
        raise StoreError("Session controls do not accept an update key.")
    text = (
        sys.stdin.read(MAX_INPUT_CHARACTERS + 1).strip()
        if args.mode in {"text", "voice"}
        else ""
    )
    if args.mode in {"text", "voice"} and (
        not text or len(text) > MAX_INPUT_CHARACTERS
    ):
        raise StoreError(
            "The update must contain 1 to 3,500 characters on standard input."
        )
    database_path = Path(required_environment("TELEGRAM_CONTROL_DB"))
    agent_id = required_environment("TELEGRAM_CONTROL_AGENT_ID")
    mailbox_id = int(required_environment("TELEGRAM_CONTROL_MAILBOX_ID"))
    worker_id = required_environment("TELEGRAM_CONTROL_WORKER_ID")
    with DurableStore(database_path) as store:
        if args.mode == "controls":
            operation_id = f"agent-mailbox:{mailbox_id}:session-controls"
            existing_state = store.outbox_operation_state(operation_id)
            if existing_state is not None:
                print(f"Telegram session controls already {existing_state}.")
                return 0
            store.enqueue_agent_session_controls(
                operation_id=operation_id,
                agent_id=agent_id,
                mailbox_id=mailbox_id,
                worker_id=worker_id,
            )
            print("Telegram session controls queued.")
            return 0
        operation_id = notification_operation_id(
            mailbox_id,
            args.mode,
            args.key,
            text,
        )
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
    except (OSError, ValueError, StoreError, voice_responses.VoiceResponseError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)

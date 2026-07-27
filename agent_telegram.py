#!/usr/bin/python3
"""Send a scoped Telegram update from the current managed agent turn."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
from pathlib import Path

import detached_worker
import provider_defaults
import telegram_bridge
import voice_responses
from durable_store import DurableStore, StoreError, validate_provider_config


MAX_INPUT_CHARACTERS = 3_500
MAX_TOPIC_PROMPT_CHARACTERS = 8_000
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
    ask_parser = subparsers.add_parser(
        "ask",
        help="Ask the owner one question with buttons (question on stdin).",
    )
    ask_parser.add_argument(
        "--key",
        required=True,
        help="Stable lowercase idempotency key for this question.",
    )
    ask_parser.add_argument(
        "--option",
        action="append",
        required=True,
        dest="options",
        metavar="LABEL",
        help="A button label; repeat for 2 to 5 options.",
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
    topic_parser = subparsers.add_parser(
        "topic-create",
        help="Create and optionally start a conversational topic.",
    )
    topic_parser.add_argument(
        "--key",
        required=True,
        help="Stable lowercase idempotency key for this creation request.",
    )
    topic_parser.add_argument(
        "--name",
        required=True,
        help="Telegram topic name.",
    )
    topic_parser.add_argument(
        "--provider",
        choices=("codex", "claude"),
        help="Provider override; omit to inherit the group default.",
    )
    topic_parser.add_argument(
        "--model",
        help="Explicit model override.",
    )
    topic_parser.add_argument(
        "--effort",
        help="Explicit effort override.",
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


def normalized_topic_name(value: str) -> str:
    name = " ".join(str(value).strip().split())
    if (
        not name
        or len(name) > 128
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in name
        )
    ):
        raise StoreError(
            "A Telegram topic name must contain 1 to 128 plain-text characters."
        )
    return name


def create_conversational_topic(
    *,
    store: DurableStore,
    agent_id: str,
    mailbox_id: int,
    worker_id: str,
    key: str,
    name: str,
    provider: str | None,
    model: str | None,
    effort: str | None,
    first_prompt: str,
) -> dict:
    if not KEY_PATTERN.fullmatch(key) or len(key) > 40:
        raise StoreError(
            "The topic key must be 1 to 40 lowercase letters, numbers, or "
            "hyphens."
        )
    display_name = normalized_topic_name(name)
    prompt = str(first_prompt).strip()
    if len(prompt) > MAX_TOPIC_PROMPT_CHARACTERS:
        raise StoreError(
            "A new topic's first prompt must contain at most 8,000 characters."
        )

    context = store.agent_topic_creation_context(
        agent_id=agent_id,
        mailbox_id=mailbox_id,
        worker_id=worker_id,
    )
    if context.chat_id >= 0 or context.message_thread_id <= 0:
        raise StoreError(
            "A conversational topic can only be created from a managed topic "
            "inside a bound private forum group."
        )
    workspace = store.resolve_forum_workspace(context.chat_id)
    if workspace is None:
        raise StoreError(
            "This Telegram group is not bound to a workspace."
        )

    selected_provider = provider or workspace.provider
    provider_config = (
        dict(workspace.provider_config)
        if selected_provider == workspace.provider
        else {}
    )
    if model is not None:
        provider_config["model"] = model
    if effort is not None:
        provider_config["effort"] = effort
    provider_config = validate_provider_config(
        selected_provider,
        provider_config,
    )
    operation_id = (
        f"agent-topic-create:{abs(int(context.chat_id))}:{key}"
    )
    plan = {
        "chat_id": context.chat_id,
        "display_name": display_name,
        "first_prompt": prompt,
        "provider": selected_provider,
        "provider_config": provider_config,
    }
    plan_digest = hashlib.sha256(
        json.dumps(
            plan,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()

    created = False
    operation_subject = store.resolve_forum_subject_creation(
        context.chat_id,
        operation_id,
    )
    if operation_subject is not None:
        if operation_subject.display_name != display_name:
            raise StoreError(
                "That topic key already belongs to a different topic name."
            )
        subject, _ = store.ensure_forum_subject(
            chat_id=context.chat_id,
            message_thread_id=operation_subject.message_thread_id,
            display_name=display_name,
            provider=selected_provider,
            provider_config=provider_config,
            creation_operation_id=operation_id,
            creation_plan_digest=plan_digest,
        )
    else:
        matching = [
            binding
            for binding in store.list_topic_surfaces(context.chat_id)
            if binding.display_name.casefold() == display_name.casefold()
        ]
        if len(matching) > 1:
            raise StoreError(
                "More than one managed topic already uses that name."
            )
    if operation_subject is None and matching:
        binding = matching[0]
        if (
            binding.target_type != "agent"
            or binding.message_thread_id is None
        ):
            raise StoreError(
                "A managed topic with that name already exists."
            )
        subject = store.resolve_forum_subject(
            context.chat_id,
            binding.message_thread_id,
        )
        if subject is None:
            raise StoreError(
                "A non-conversational managed topic with that name already "
                "exists."
            )
        subject, _ = store.ensure_forum_subject(
            chat_id=context.chat_id,
            message_thread_id=binding.message_thread_id,
            display_name=display_name,
            provider=selected_provider,
            provider_config=provider_config,
            creation_operation_id=operation_id,
            creation_plan_digest=plan_digest,
        )
    elif operation_subject is None:
        token = telegram_bridge.read_token()
        bot = telegram_bridge.api_call(token, "getMe")
        member = telegram_bridge.api_call(
            token,
            "getChatMember",
            chat_id=context.chat_id,
            user_id=int(bot["id"]),
        )
        status = str(member.get("status", ""))
        if status not in {"administrator", "creator"} or (
            status == "administrator"
            and member.get("can_manage_topics") is not True
        ):
            raise StoreError(
                "The Telegram bot must be a group administrator with Manage "
                "topics permission."
            )

        # Revalidate the active lease and its home group immediately before
        # crossing the external mutation boundary.
        current_context = store.agent_topic_creation_context(
            agent_id=agent_id,
            mailbox_id=mailbox_id,
            worker_id=worker_id,
        )
        if current_context != context:
            raise StoreError(
                "The managed turn changed while preparing the new topic."
            )
        topic = telegram_bridge.api_call(
            token,
            "createForumTopic",
            chat_id=context.chat_id,
            name=display_name,
        )
        try:
            message_thread_id = int(topic["message_thread_id"])
        except (KeyError, TypeError, ValueError):
            raise StoreError(
                "Telegram returned an invalid forum-topic result."
            ) from None
        subject, created = store.ensure_forum_subject(
            chat_id=context.chat_id,
            message_thread_id=message_thread_id,
            display_name=display_name,
            provider=selected_provider,
            provider_config=provider_config,
            creation_operation_id=operation_id,
            creation_plan_digest=plan_digest,
        )
        binding = store.resolve_surface_binding(
            context.chat_id,
            message_thread_id,
        )
        if binding is None:
            raise StoreError(
                "The new conversational topic has no durable route."
            )

    store.enqueue_forum_subject_intro(
        subject.agent_id,
        operation_id,
        started=bool(prompt),
    )
    mailbox = None
    if prompt:
        new_agent = store.resolve_agent(subject.agent_id)
        if new_agent is None:
            raise StoreError("The new topic's managed agent is unavailable.")
        provider_summary = provider_defaults.provider_turn_summary(
            new_agent.provider,
            new_agent.provider_config,
            new_agent.project_path,
        )
        receipt = (
            "📨 <b>Queued</b>\n"
            f"⚙️ <b>{html.escape(provider_summary)}</b>"
        )
        mailbox = store.enqueue_agent_generated_prompt(
            agent_id=subject.agent_id,
            operation_id=operation_id,
            input_text=prompt,
            chat_id=context.chat_id,
            message_thread_id=subject.message_thread_id,
            authorized_user_id=context.authorized_user_id,
            receipt_text=receipt,
            receipt_parse_mode="HTML",
        )

    final_binding = store.resolve_surface_binding(
        context.chat_id,
        subject.message_thread_id,
    )
    return {
        "agent_id": subject.agent_id,
        "created": created,
        "display_name": display_name,
        "first_prompt_queued": mailbox is not None,
        "mailbox_id": mailbox,
        "message_thread_id": subject.message_thread_id,
        "provider": selected_provider,
        "provider_config": provider_config,
        "topic_url": detached_worker.telegram_topic_url(final_binding),
    }


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
    if args.mode == "topic-create":
        first_prompt = sys.stdin.read(MAX_TOPIC_PROMPT_CHARACTERS + 1)
        with DurableStore(database_path) as store:
            result = create_conversational_topic(
                store=store,
                agent_id=agent_id,
                mailbox_id=mailbox_id,
                worker_id=worker_id,
                key=args.key,
                name=args.name,
                provider=args.provider,
                model=args.model,
                effort=args.effort,
                first_prompt=first_prompt,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if not KEY_PATTERN.fullmatch(args.key) or len(args.key) > 40:
        raise StoreError(
            "The update key must be 1 to 40 lowercase letters, numbers, or hyphens."
        )
    if args.mode == "ask":
        question = sys.stdin.read(MAX_INPUT_CHARACTERS + 1).strip()
        with DurableStore(database_path) as store:
            store.enqueue_agent_choice_prompt(
                agent_id=agent_id,
                mailbox_id=mailbox_id,
                worker_id=worker_id,
                key=args.key,
                question=question,
                options=args.options,
            )
        print("Telegram question queued.")
        return 0
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
            voice_configuration = store.voice_configuration()
            voice_path = voice_responses.synthesize_voice(
                text,
                f"mailbox-{mailbox_id}-{args.key}-"
                f"{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}",
                protected_paths=store.pending_voice_file_paths(),
                voice_name=voice_configuration.voice_name,
                rate=voice_configuration.rate,
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

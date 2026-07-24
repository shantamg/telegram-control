#!/usr/bin/python3
"""Handle authorized Telegram text and voice messages."""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import telegram_bridge as bridge
import tmux_console
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
    card: Optional[dict] = None,
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
                card=card,
            )
        return

    token = bridge.read_token()
    bridge.api_call(token, method, **params)


def controller_reply_route() -> dict:
    route = {
        "target_type": "controller",
        "target_id": "control",
        "policy": "reply",
        "ttl_seconds": 30 * 24 * 60 * 60,
    }
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not database_path or not chat_id:
        return route
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    thread_id = int(thread_id_text) if thread_id_text else None
    with DurableStore(Path(database_path)) as store:
        binding = store.resolve_surface_binding(
            chat_id=int(chat_id),
            message_thread_id=thread_id,
        )
    if binding is not None:
        route["target_type"] = binding.target_type
        route["target_id"] = binding.target_id
    return route


def current_surface_binding():
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not database_path or not chat_id:
        return None
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    thread_id = int(thread_id_text) if thread_id_text else None
    with DurableStore(Path(database_path)) as store:
        return store.resolve_surface_binding(
            chat_id=int(chat_id),
            message_thread_id=thread_id,
        )


def surface_coordinates():
    chat_id = int(os.environ["TELEGRAM_CHAT_ID"])
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    thread_id = int(thread_id_text) if thread_id_text else None
    return chat_id, thread_id


def refresh_keyboard(token: str) -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": "Refresh",
                    "callback_data": f"a:{token}",
                }
            ]
        ]
    }


def status_card_text(store: DurableStore, binding, refresh_marker: str) -> str:
    counts = store.status_counts()
    accepted_updates = sum(counts["updates"].values())
    active_routes = counts["routes"].get("active", 0)
    active_callbacks = counts["callbacks"].get("active", 0)
    surface = (
        binding.display_name
        if binding.message_thread_id is None
        else f"{binding.display_name} · topic {binding.message_thread_id}"
    )
    return (
        "Telegram Control\n\n"
        f"Database: {store.quick_check()}\n"
        f"Surface: {surface}\n"
        f"Target: {binding.target_type}/{binding.target_id}\n"
        f"Stored updates: {accepted_updates}\n"
        f"Active return routes: {active_routes}\n"
        f"Active buttons: {active_callbacks}\n"
        f"Refresh: {refresh_marker}"
    )


def send_status_card(update: dict) -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    if not database_path or not job_id:
        send_message("Durable status requires the Stage 2 controller.")
        return
    chat_id, thread_id = surface_coordinates()
    user_id = int(os.environ["TELEGRAM_FROM_ID"])
    with DurableStore(Path(database_path)) as store:
        binding = store.resolve_surface_binding(
            chat_id=chat_id,
            message_thread_id=thread_id,
        )
        if binding is None:
            if thread_id is not None:
                raise StoreError("This topic has no durable controller binding.")
            binding = store.ensure_surface_binding(
                chat_id=chat_id,
                message_thread_id=thread_id,
                surface_type="control",
                display_name="Control",
                target_type="controller",
                target_id="control",
            )
        action = store.create_callback_action(
            operation_id=f"surface:{binding.binding_id}:status-refresh",
            action_type="refresh_status",
            payload={"binding_id": binding.binding_id},
            chat_id=chat_id,
            message_thread_id=thread_id,
            authorized_user_id=user_id,
            one_time=False,
            ttl_seconds=30 * 24 * 60 * 60,
        )
        card, created = store.ensure_surface_card(
            binding_id=binding.binding_id,
            card_type="status",
            callback_action_id=action.action_id,
        )
        text = status_card_text(store, binding, f"created by update {update['update_id']}")
    params = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": refresh_keyboard(action.token),
    }
    if card.state == "active" and card.telegram_message_id is not None:
        params["message_id"] = card.telegram_message_id
        deliver_api_call(
            "editMessageText",
            params,
            "status-card-edit",
            card={"card_id": card.card_id, "mode": "edit"},
        )
    elif created:
        params["message_thread_id"] = thread_id
        deliver_api_call(
            "sendMessage",
            params,
            f"status-card-create:{card.generation}",
            route=controller_reply_route(),
            card={"card_id": card.card_id, "mode": "activate"},
        )


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


def send_message(
    text: str,
    include_inspect_button: bool = False,
    reply_markup: Optional[dict] = None,
) -> None:
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
    if include_inspect_button:
        reply_markup = inspect_keyboard()
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


def send_agent_status() -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    if not database_path:
        send_message("Managed agent status requires the durable controller.")
        return
    chat_id, thread_id = surface_coordinates()
    with DurableStore(Path(database_path)) as store:
        agent = store.resolve_agent_for_surface(chat_id, thread_id)
        console = (
            tmux_console.reconcile_agent_console(store, agent.agent_id)
            if agent is not None
            else None
        )
        usage = (
            store.latest_agent_usage(agent.agent_id)
            if agent is not None
            else None
        )
        job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
        user_id = os.environ.get("TELEGRAM_FROM_ID")
        keyboard = None
        if agent is not None and job_id and user_id:
            action_type = (
                "agent_resume"
                if agent.lifecycle_state == "stopped"
                else "agent_pause"
            )
            action_label = (
                "▶️ Resume"
                if agent.lifecycle_state == "stopped"
                else "⏸ Pause"
            )
            lifecycle_action = store.create_callback_action(
                operation_id=f"inbox:{job_id}:{action_type}",
                action_type=action_type,
                payload={"agent_id": agent.agent_id},
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=int(user_id),
                one_time=True,
                ttl_seconds=15 * 60,
            )
            new_session_action = store.create_callback_action(
                operation_id=f"inbox:{job_id}:agent-new-session-prompt",
                action_type="agent_new_session_prompt",
                payload={"agent_id": agent.agent_id},
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=int(user_id),
                one_time=True,
                ttl_seconds=15 * 60,
            )
            keyboard = {
                "inline_keyboard": [
                    [
                        {
                            "text": action_label,
                            "callback_data": f"a:{lifecycle_action.token}",
                        },
                        {
                            "text": "New session…",
                            "callback_data": f"a:{new_session_action.token}",
                        },
                    ]
                ]
            }
    if agent is None:
        send_message("This Telegram surface has no managed agent.")
        return
    project_name = Path(agent.project_path).name if agent.project_path else "controller"
    session = "not started" if not agent.provider_session_id else "persisted"
    console_state = console.state if console is not None else "stopped"
    usage_line = ""
    if usage is not None:
        input_tokens = int(usage.get("input_tokens", 0))
        cached_tokens = int(usage.get("cached_input_tokens", 0))
        output_tokens = int(usage.get("output_tokens", 0))
        usage_line = (
            "\nLast turn: "
            f"{input_tokens:,} input ({cached_tokens:,} cached) · "
            f"{output_tokens:,} output"
        )
    send_message(
        "Managed agent\n\n"
        f"Name: {agent.hierarchical_name}\n"
        f"Role: {agent.role}\n"
        f"Provider: {agent.provider}\n"
        f"Project: {project_name}\n"
        f"State: {agent.lifecycle_state}\n"
        f"Session: {session}\n"
        f"Console: {console_state}"
        f"{usage_line}",
        reply_markup=keyboard,
    )


def send_project_catalog() -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    if not database_path:
        send_message("Project catalog requires the durable controller.")
        return
    with DurableStore(Path(database_path)) as store:
        projects = store.list_projects()
    if not projects:
        send_message("No local projects are enrolled.")
        return
    lines = ["Enrolled projects", ""]
    for project in projects:
        lines.append(
            f"{project.slug} — {project.display_name} ({project.provider})"
        )
    lines.extend(
        [
            "",
            "Inside a provisioned project topic, send:",
            "/agent create <slug>",
        ]
    )
    send_message("\n".join(lines))


def create_agent_from_catalog(project_slug: str) -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    if not database_path:
        raise StoreError("Project creation requires the durable controller.")
    chat_id, thread_id = surface_coordinates()
    try:
        with DurableStore(Path(database_path)) as store:
            agent, created = store.attach_enrolled_project(
                chat_id,
                thread_id,
                project_slug,
            )
    except StoreError as exc:
        send_message(f"❌ {exc}")
        return
    if created:
        send_message(
            f"✅ Created managed agent {agent.hierarchical_name}.\n"
            "Send a message in this topic to start its provider session."
        )
    else:
        send_message(
            f"✅ {agent.hierarchical_name} is already attached to this topic."
        )


def enqueue_agent_input(agent_id: str, text: str) -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    if not database_path or not job_id:
        raise StoreError("Managed agent input requires the durable controller.")
    with DurableStore(Path(database_path)) as store:
        agent = store.resolve_agent(agent_id)
        if agent is None or agent.role not in {"project", "worker"}:
            raise StoreError("Managed agent route is no longer valid.")
        chat_id, thread_id = surface_coordinates()
        receipt = "⏳ <b>Working…</b>"
        store.enqueue_agent_message_with_receipt(
            agent_id=agent.agent_id,
            source_inbox_job_id=int(job_id),
            input_text=text,
            chat_id=chat_id,
            message_thread_id=thread_id,
            receipt_text=receipt,
            receipt_parse_mode="HTML",
        )


def enqueue_router_input(text: str) -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    if not database_path or not job_id:
        raise StoreError("Main-router input requires the durable controller.")
    chat_id, thread_id = surface_coordinates()
    with DurableStore(Path(database_path)) as store:
        binding = store.resolve_surface_binding(chat_id, thread_id)
        if binding is None:
            binding = store.ensure_surface_binding(
                chat_id=chat_id,
                message_thread_id=thread_id,
                surface_type="control",
                display_name="Control",
                target_type="controller",
                target_id="control",
            )
        if (
            binding.surface_type != "control"
            or binding.target_type != "controller"
            or binding.target_id != "control"
        ):
            raise StoreError("Main router surface is no longer valid.")
        store.enqueue_router_message_with_receipt(
            source_inbox_job_id=int(job_id),
            input_text=text,
            chat_id=chat_id,
            message_thread_id=thread_id,
            authorized_user_id=int(os.environ["TELEGRAM_FROM_ID"]),
            receipt_text="🧭 <b>Routing…</b>",
            receipt_parse_mode="HTML",
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

    if action.action_type == "inspect_status":
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Durable route verified.",
                },
                "callback-answer",
            )
        send_message(
            "✅ Durable button route verified.\n\n"
            "The opaque action was authorized, resolved from SQLite, and "
            "consumed exactly once."
        )
        return
    if action.action_type == "router_clarification":
        router_mailbox_id = int(action.payload.get("router_mailbox_id", 0))
        choice = str(action.payload.get("choice", ""))
        with DurableStore(Path(database_path)) as store:
            clarified_input = store.resolve_router_clarification(
                router_mailbox_id,
                choice,
            )
            store.enqueue_router_message_with_receipt(
                source_inbox_job_id=int(os.environ["TELEGRAM_CONTROL_JOB_ID"]),
                input_text=clarified_input,
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=user_id,
                receipt_text="🧭 <b>Routing…</b>",
                receipt_parse_mode="HTML",
            )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": f"Selected: {choice}",
                },
                "callback-answer",
            )
        deliver_api_call(
            "editMessageReplyMarkup",
            {
                "chat_id": chat_id,
                "message_id": int(os.environ["TELEGRAM_MESSAGE_ID"]),
                "reply_markup": {"inline_keyboard": []},
            },
            "clarification-clear",
        )
        return
    if action.action_type in {
        "router_project_confirm",
        "router_project_cancel",
    }:
        router_mailbox_id = int(action.payload.get("router_mailbox_id", 0))
        with DurableStore(Path(database_path)) as store:
            store.expire_router_project_actions(router_mailbox_id)
        if action.action_type == "router_project_cancel":
            if callback_query_id:
                deliver_api_call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_query_id,
                        "text": "Cancelled.",
                    },
                    "callback-answer",
                )
            send_message("Cancelled. No project or agent was created.")
            return

        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Creating project agent…",
                },
                "callback-answer",
            )
        required = {
            "slug",
            "display_name",
            "provider",
            "project_path",
            "topic_name",
        }
        if not required.issubset(action.payload):
            raise StoreError("Stored project-creation plan is invalid.")
        requested_path = Path(str(action.payload["project_path"])).resolve()
        if not requested_path.is_dir():
            raise StoreError("The confirmed project directory no longer exists.")
        git_result = subprocess.run(
            ["git", "-C", str(requested_path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if (
            git_result.returncode != 0
            or Path(git_result.stdout.strip()).resolve() != requested_path
        ):
            raise StoreError(
                "The confirmed path is no longer the validated Git repository."
            )
        slug = str(action.payload["slug"])
        display_name = str(action.payload["display_name"])
        provider = str(action.payload["provider"])
        topic_name = str(action.payload["topic_name"])
        with DurableStore(Path(database_path)) as store:
            store.enroll_project(
                slug=slug,
                display_name=display_name,
                provider=provider,
                project_path=str(requested_path),
            )
            existing_agent = store.resolve_project_agent(slug)
            existing_surface = store.resolve_named_surface(
                chat_id,
                topic_name,
                surface_type="project",
            )
        if existing_agent is not None:
            send_message(
                f"✅ {display_name} already has managed agent "
                f"{existing_agent.hierarchical_name}."
            )
            return
        if existing_surface is None:
            topic = bridge.api_call(
                bridge.read_token(),
                "createForumTopic",
                chat_id=chat_id,
                name=topic_name,
            )
            try:
                project_thread_id = int(topic["message_thread_id"])
            except (KeyError, TypeError, ValueError):
                raise StoreError(
                    "Telegram returned an invalid project-topic result."
                ) from None
            with DurableStore(Path(database_path)) as store:
                store.ensure_surface_binding(
                    chat_id=chat_id,
                    message_thread_id=project_thread_id,
                    surface_type="project",
                    display_name=topic_name,
                    target_type="controller",
                    target_id="control",
                )
        with DurableStore(Path(database_path)) as store:
            agent, _ = store.attach_enrolled_project(
                chat_id,
                (
                    existing_surface.message_thread_id
                    if existing_surface is not None
                    else project_thread_id
                ),
                slug,
            )
        send_message(
            f"✅ Created {agent.hierarchical_name} in the "
            f"{topic_name} project topic."
        )
        return
    if action.action_type in {"agent_pause", "agent_resume"}:
        agent_id = str(action.payload.get("agent_id", ""))
        try:
            with DurableStore(Path(database_path)) as store:
                bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
                if bound_agent is None or bound_agent.agent_id != agent_id:
                    raise StoreError("Managed agent surface changed.")
                if action.action_type == "agent_pause":
                    store.pause_agent(agent_id)
                    result_text = "⏸ Agent paused."
                else:
                    store.resume_agent(agent_id)
                    result_text = "▶️ Agent resumed."
        except StoreError as exc:
            if callback_query_id:
                deliver_api_call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_query_id,
                        "text": str(exc),
                        "show_alert": True,
                    },
                    "callback-answer",
                )
            return
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": result_text,
                },
                "callback-answer",
            )
        send_message(result_text + " Send /agent to inspect or change it.")
        return
    if action.action_type == "agent_new_session_prompt":
        agent_id = str(action.payload.get("agent_id", ""))
        with DurableStore(Path(database_path)) as store:
            bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
            if bound_agent is None or bound_agent.agent_id != agent_id:
                raise StoreError("Managed agent surface changed.")
            confirm = store.create_callback_action(
                operation_id=f"callback:{update['update_id']}:agent-new-session-confirm",
                action_type="agent_new_session_confirm",
                payload={"agent_id": agent_id},
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=user_id,
                one_time=True,
                ttl_seconds=5 * 60,
            )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Confirmation required.",
                },
                "callback-answer",
            )
        send_message(
            "Start a fresh Codex conversation?\n\n"
            "The current conversation is retained by Codex, but new messages "
            "will no longer continue it.",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "Confirm new session",
                            "callback_data": f"a:{confirm.token}",
                        }
                    ]
                ]
            },
        )
        return
    if action.action_type == "agent_new_session_confirm":
        agent_id = str(action.payload.get("agent_id", ""))
        try:
            with DurableStore(Path(database_path)) as store:
                bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
                if bound_agent is None or bound_agent.agent_id != agent_id:
                    raise StoreError("Managed agent surface changed.")
                store.reset_agent_session(agent_id)
        except StoreError as exc:
            if callback_query_id:
                deliver_api_call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_query_id,
                        "text": str(exc),
                        "show_alert": True,
                    },
                    "callback-answer",
                )
            return
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "New session ready.",
                },
                "callback-answer",
            )
        send_message("✅ The next message will start a fresh Codex conversation.")
        return
    if action.action_type == "refresh_status":
        chat_id, thread_id = surface_coordinates()
        with DurableStore(Path(database_path)) as store:
            binding = store.resolve_surface_binding(
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
            if (
                binding is None
                or binding.binding_id != int(action.payload.get("binding_id", 0))
                or binding.target_type != "controller"
                or binding.target_id != "control"
            ):
                raise StoreError("Status card surface binding is no longer valid.")
            card = store.resolve_surface_card(binding.binding_id, "status")
            if (
                card is None
                or card.state != "active"
                or card.callback_action_id != action.action_id
                or card.telegram_message_id
                != int(os.environ["TELEGRAM_MESSAGE_ID"])
            ):
                if callback_query_id:
                    deliver_api_call(
                        "answerCallbackQuery",
                        {
                            "callback_query_id": callback_query_id,
                            "text": "This status card was replaced. Send /status.",
                        },
                        "callback-answer",
                    )
                return
            text = status_card_text(
                store,
                binding,
                f"update {update['update_id']} at {time.strftime('%H:%M:%S')}",
            )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Status refreshed.",
                },
                "callback-answer",
            )
        deliver_api_call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": int(os.environ["TELEGRAM_MESSAGE_ID"]),
                "text": text,
                "reply_markup": refresh_keyboard(action.token),
            },
            "status-edit",
            card={"card_id": card.card_id, "mode": "edit"},
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

    binding = current_surface_binding()
    managed_agent = None
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    if (
        binding is not None
        and binding.target_type == "agent"
        and database_path
        and job_id
    ):
        with DurableStore(Path(database_path)) as store:
            managed_agent = store.resolve_agent(binding.target_id)
            if managed_agent is None or managed_agent.role not in {
                "project",
                "worker",
            }:
                raise StoreError("Managed agent route is no longer valid.")
            chat_id, thread_id = surface_coordinates()
            store.enqueue_agent_receipt(
                agent_id=managed_agent.agent_id,
                source_inbox_job_id=int(job_id),
                chat_id=chat_id,
                message_thread_id=thread_id,
                receipt_text="🎙️ <b>Transcribing…</b>",
                input_kind="voice",
                parse_mode="HTML",
            )
    else:
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

    if managed_agent is not None:
        agent_input = transcript or (
            "The user's voice note contained no detectable speech. "
            "Briefly ask them to try recording it again."
        )
        with DurableStore(Path(database_path)) as store:
            store.enqueue_agent_voice_message(
                agent_id=managed_agent.agent_id,
                source_inbox_job_id=int(job_id),
                input_text=agent_input,
            )
    elif transcript:
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
            agent_create = re.fullmatch(
                r"/agent\s+create\s+([a-z0-9]+(?:-[a-z0-9]+)*)",
                text.strip().lower(),
            )
            if text.strip().lower() == "/status":
                send_status_card(update)
            elif text.strip().lower() == "/projects":
                send_project_catalog()
            elif agent_create is not None:
                create_agent_from_catalog(agent_create.group(1))
            elif text.strip().lower() in {"/agent", "/agent status"}:
                send_agent_status()
            elif replied_message_id:
                route = resolve_replied_message_route()
                if (
                    route is not None
                    and route.target_type == "controller"
                    and route.target_id == "control"
                ):
                    enqueue_router_input(text)
                elif route is not None and route.target_type == "agent":
                    enqueue_agent_input(route.target_id, text)
                else:
                    send_message(
                        "That replied-to message has no active durable route."
                    )
            else:
                binding = current_surface_binding()
                thread_id = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID")
                if thread_id and binding is None:
                    send_message("This topic has no durable controller binding.")
                elif thread_id and binding is not None:
                    if binding.target_type == "agent":
                        enqueue_agent_input(binding.target_id, text)
                    else:
                        send_message(
                            f"✅ {binding.display_name} route verified: {text}",
                            include_inspect_button=True,
                        )
                else:
                    enqueue_router_input(text)
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
        send_message(f"❌ {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/python3
"""Handle authorized Telegram text, image, and voice messages."""

import html
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import claude_sessions
import codex_sessions
import detached_worker
import discovery
import helper_paths
import provider_adapters
import provider_defaults
import router_contract
import telegram_bridge as bridge
import telegram_help
import tmux_console
import voice_responses
from durable_store import (
    CallbackActionError,
    DurableStore,
    StoreError,
    chunk_telegram_text,
    compose_forum_setup_input,
    context_usage_summary,
)


HANDY_BINARY = helper_paths.resolve_binary(
    "handy_binary",
    Path("/Applications/Handy.app/Contents/MacOS/handy"),
    command_name="handy",
)
PARAKEET_MODEL_ID = "parakeet-tdt-0.6b-v3"
FFMPEG_BINARY = helper_paths.resolve_binary(
    "ffmpeg_binary",
    Path("/opt/homebrew/bin/ffmpeg"),
    Path("/usr/local/bin/ffmpeg"),
    command_name="ffmpeg",
)
MAX_VOICE_BYTES = 20_000_000
MAX_VOICE_SECONDS = 30 * 60
MAX_ATTACHMENT_BYTES = 20_000_000
TRANSCRIPTION_TIMEOUT_SECONDS = 15 * 60
TELEGRAM_TEXT_CHUNK = 3_800
OUTPUT_SEQUENCE = 0
CONTROL_SPEAKER = "🎛 Control"
# Binding a folder is the one thing a new group cannot infer, so every entry
# point asks for it directly instead of waiting to be told.
WORKSPACE_QUESTION = (
    "Which folder should this group work in? Reply with a path like "
    "~/Software/my-project, or just describe the project and I will find it "
    "and confirm before binding anything."
)
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


def current_speaker_header() -> str:
    """Controller-authored messages always identify Control as the source."""
    return CONTROL_SPEAKER


def speaker_labeled_text(text: str) -> str:
    """Label every controller-authored response without double-prefixing."""
    content = text.strip() or "[empty controller response]"
    if content.startswith(CONTROL_SPEAKER):
        return content
    speaker = current_speaker_header()
    if content == speaker or content.startswith(f"{speaker}\n"):
        return content
    return f"{speaker}\n\n{content}"


def surface_coordinates():
    chat_id = int(os.environ["TELEGRAM_CHAT_ID"])
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    thread_id = int(thread_id_text) if thread_id_text else None
    return chat_id, thread_id


def surface_display_name() -> str:
    return (
        os.environ.get("TELEGRAM_TOPIC_NAME")
        or os.environ.get("TELEGRAM_CHAT_TITLE")
        or "Control"
    )


def handle_report_only_topic() -> bool:
    """Answer worker-topic input by policy before any conversational routing."""
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    chat_id_text = os.environ.get("TELEGRAM_CHAT_ID")
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID")
    if not database_path or not chat_id_text or not thread_id_text:
        return False
    with DurableStore(Path(database_path)) as store:
        worker = store.detached_worker_for_thread(
            int(chat_id_text),
            int(thread_id_text),
        )
        if worker is None:
            return False
        origin = detached_worker.origin_surface(store, worker)
        notice = detached_worker.report_only_notice(
            worker,
            origin.display_name if origin is not None else None,
            detached_worker.telegram_topic_url(origin),
        )
    send_message(notice)
    return True


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
    router = store.router_session_metrics()
    accepted_updates = sum(counts["updates"].values())
    active_routes = counts["routes"].get("active", 0)
    active_callbacks = counts["callbacks"].get("active", 0)
    surface = (
        binding.display_name
        if binding.message_thread_id is None
        else f"{binding.display_name} · topic {binding.message_thread_id}"
    )
    return (
        f"{CONTROL_SPEAKER}\n\nTelegram Control\n\n"
        f"Database: {store.quick_check()}\n"
        f"Surface: {surface}\n"
        f"Target: {binding.target_type}/{binding.target_id}\n"
        f"Stored updates: {accepted_updates}\n"
        f"Active return routes: {active_routes}\n"
        f"Active buttons: {active_callbacks}\n"
        f"Router session: "
        f"{'persisted' if router['provider_session_id'] else 'not started'}\n"
        f"Router context: {router['completed_turns']} turns · "
        f"{router['input_tokens']:,} input tokens · "
        f"{store.router_rotation_count()} rotations\n"
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
            binding = store.ensure_surface_binding(
                chat_id=chat_id,
                message_thread_id=thread_id,
                surface_type="control",
                display_name=surface_display_name(),
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


def bot_username() -> str:
    """Resolve the paired bot's username for user-facing copy and links."""
    username = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip()
    if username:
        return username
    try:
        return str(bridge.load_config().get("bot_username", "")).strip()
    except bridge.BridgeError:
        return ""


def addressed_command(text: str) -> str:
    """Strip Telegram's @botname suffix from a command addressed to this bot.

    Tapping a command in a group from the registered menu inserts
    `/agent@yourbot`, because a group can hold several bots. Without this the
    text misses every command comparison and is handled as an ordinary message —
    the agent answers instead of the controller acting. A command addressed to a
    different bot is left untouched so it is not treated as ours.
    """
    stripped = text.strip()
    if not stripped.startswith("/"):
        return stripped
    head, _, rest = stripped.partition(" ")
    command, mention_marker, mention = head.partition("@")
    if mention_marker:
        username = bot_username()
        if not username or mention.casefold() != username.casefold():
            return stripped
    rest = rest.strip()
    return f"{command} {rest}" if rest else command


def bot_label() -> str:
    """Name this bot the way the owner sees it, without hardcoding an identity."""
    username = bot_username()
    return f"@{username}" if username else "this bot"


def send_message(
    text: str,
    include_inspect_button: bool = False,
    reply_markup: Optional[dict] = None,
    card: Optional[dict] = None,
) -> None:
    global OUTPUT_SEQUENCE
    chat_id_text = os.environ.get("TELEGRAM_CHAT_ID")
    if chat_id_text:
        chat_id = int(chat_id_text)
    else:
        chat_id = int(bridge.load_config()["chat_id"])
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    thread_id = int(thread_id_text) if thread_id_text else None
    labeled = speaker_labeled_text(text)
    if "\n\n" in labeled:
        speaker, payload = labeled.split("\n\n", 1)
    else:
        speaker, payload = current_speaker_header(), labeled
    budget = max(1000, TELEGRAM_TEXT_CHUNK - len(speaker) - 2)
    chunks = chunk_telegram_text(payload, limit=budget)
    if include_inspect_button:
        reply_markup = inspect_keyboard()
    for index, payload_chunk in enumerate(chunks):
        chunk = f"{speaker}\n\n{payload_chunk}"
        OUTPUT_SEQUENCE += 1
        params = {
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "text": chunk,
        }
        last_chunk = index == len(chunks) - 1
        if last_chunk and reply_markup is not None:
            params["reply_markup"] = reply_markup
        deliver_api_call(
            "sendMessage",
            params,
            f"message:{OUTPUT_SEQUENCE}",
            route=controller_reply_route(),
            # Only the first chunk carries card metadata: it is the message
            # the topic remembers as its header.
            card=card if index == 0 else None,
        )


def help_reply_markup(
    store: DurableStore,
    *,
    menu_id: str,
    chat_id: int,
    thread_id: Optional[int],
    user_id: int,
    current_slug: str,
) -> dict:
    actions = []
    for topic in telegram_help.TOPICS:
        action = store.create_callback_action(
            operation_id=f"help:{menu_id}:{topic.slug}",
            action_type="help_topic",
            payload={"menu_id": menu_id, "topic": topic.slug},
            chat_id=chat_id,
            message_thread_id=thread_id,
            authorized_user_id=user_id,
            one_time=False,
            ttl_seconds=30 * 24 * 60 * 60,
        )
        actions.append((topic.label, action))
    rows = option_button_rows(actions, width=2)
    if current_slug != "home":
        home = store.create_callback_action(
            operation_id=f"help:{menu_id}:home",
            action_type="help_topic",
            payload={"menu_id": menu_id, "topic": "home"},
            chat_id=chat_id,
            message_thread_id=thread_id,
            authorized_user_id=user_id,
            one_time=False,
            ttl_seconds=30 * 24 * 60 * 60,
        )
        rows.append(
            [
                {
                    "text": "← Help menu",
                    "callback_data": f"a:{home.token}",
                }
            ]
        )
    return {"inline_keyboard": rows}


def send_help_menu() -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    user_id_text = os.environ.get("TELEGRAM_FROM_ID")
    if not database_path or not job_id or not user_id_text:
        send_message(telegram_help.HOME_TEXT)
        return
    chat_id, thread_id = surface_coordinates()
    with DurableStore(Path(database_path)) as store:
        reply_markup = help_reply_markup(
            store,
            menu_id=str(job_id),
            chat_id=chat_id,
            thread_id=thread_id,
            user_id=int(user_id_text),
            current_slug="home",
        )
    send_message(telegram_help.HOME_TEXT, reply_markup=reply_markup)


def request_topic_teardown() -> None:
    """Queue the direct /teardown confirmation without starting an LLM turn."""
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    user_id_text = os.environ.get("TELEGRAM_FROM_ID")
    if not database_path or not job_id or not user_id_text:
        raise StoreError("The /teardown command requires a managed Telegram turn.")
    chat_id, thread_id = surface_coordinates()
    if thread_id is None:
        raise StoreError("/teardown only works inside a managed topic.")
    with DurableStore(Path(database_path)) as store:
        store.enqueue_topic_teardown_prompt_for_surface(
            chat_id=chat_id,
            message_thread_id=thread_id,
            authorized_user_id=int(user_id_text),
            source_inbox_job_id=int(job_id),
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


def build_router_reply_input(update: dict, route, user_text: str) -> str:
    reply = (update.get("message") or {}).get("reply_to_message") or {}
    quoted = reply.get("text") or reply.get("caption") or ""
    provenance_label = "a controller message"
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    if database_path and route is not None:
        with DurableStore(Path(database_path)) as store:
            provenance_label = store.route_provenance_label(route.route_id)
    return router_contract.compose_reply_context_input(
        user_text,
        str(quoted),
        provenance_label,
    )


def enqueue_agent_reply_input(
    route,
    text: str,
    receipt_text: Optional[str] = None,
) -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    if not database_path or not job_id:
        raise StoreError("Managed agent input requires the durable controller.")
    chat_id, thread_id = surface_coordinates()
    with DurableStore(Path(database_path)) as store:
        speaker = store.agent_surface_header(
            route.target_id,
            chat_id,
            thread_id,
        )
        agent = store.resolve_agent(route.target_id)
        provider_summary = (
            provider_defaults.provider_turn_summary(
                agent.provider,
                agent.provider_config,
                agent.project_path,
            )
            if agent is not None
            else "Codex"
        )
        context_snapshot = store.agent_context_snapshot(route.target_id)
        metadata_line = f"\n⚙️ <b>{html.escape(provider_summary)}</b>"
        context_line = (
            "\n📊 <b>Context before this turn:</b> "
            f"{context_snapshot}"
            if context_snapshot is not None
            else ""
        )
        store.enqueue_agent_reply_message_with_receipt(
            agent_id=route.target_id,
            source_inbox_job_id=int(job_id),
            input_text=text,
            chat_id=chat_id,
            message_thread_id=thread_id,
            replied_message_id=route.telegram_message_id,
            receipt_text=receipt_text
            or (
                "📨 <b>Queued"
                + (f" for {html.escape(speaker)}" if speaker else "")
                + f"</b>{metadata_line}{context_line}"
            ),
            receipt_parse_mode="HTML",
            authorized_user_id=int(os.environ["TELEGRAM_FROM_ID"]),
        )


def try_enqueue_agent_steer(route, text: str) -> bool:
    """Treat a reply to the exact active turn card as live guidance."""
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    chat_id_text = os.environ.get("TELEGRAM_CHAT_ID")
    if not database_path or not job_id or not chat_id_text:
        raise StoreError("Managed agent steering requires the durable controller.")
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    thread_id = int(thread_id_text) if thread_id_text else None
    with DurableStore(Path(database_path)) as store:
        control = store.enqueue_agent_steer_from_receipt(
            agent_id=route.target_id,
            source_inbox_job_id=int(job_id),
            input_text=text,
            chat_id=int(chat_id_text),
            message_thread_id=thread_id,
            replied_message_id=route.telegram_message_id,
        )
    return control is not None


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
            store.current_agent_usage(agent.agent_id)
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
            resume_session_action = None
            if agent.provider in {"codex", "claude"}:
                resume_session_action = store.create_callback_action(
                    operation_id=f"inbox:{job_id}:agent-resume-session-picker",
                    action_type="agent_resume_session_picker",
                    payload={
                        "agent_id": agent.agent_id,
                        "provider": agent.provider,
                    },
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    authorized_user_id=int(user_id),
                    one_time=True,
                    ttl_seconds=15 * 60,
                )
            other_provider = "claude" if agent.provider == "codex" else "codex"
            other_provider_name = (
                "Claude" if other_provider == "claude" else "Codex"
            )
            switch_provider_action = store.create_callback_action(
                operation_id=f"inbox:{job_id}:agent-switch-provider-prompt",
                action_type="agent_switch_provider_prompt",
                payload={
                    "agent_id": agent.agent_id,
                    "expected_provider": agent.provider,
                    "provider": other_provider,
                },
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=int(user_id),
                one_time=True,
                ttl_seconds=15 * 60,
            )
            configure_action = store.create_callback_action(
                operation_id=f"inbox:{job_id}:agent-configure-picker",
                action_type="agent_configure_picker",
                payload={
                    "agent_id": agent.agent_id,
                    "provider": agent.provider,
                },
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
                    ],
                    *(
                        [
                            [
                                {
                                    "text": "Resume another session…",
                                    "callback_data": (
                                        f"a:{resume_session_action.token}"
                                    ),
                                }
                            ]
                        ]
                        if resume_session_action is not None
                        else []
                    ),
                    [
                        {
                            "text": "Change model / effort…",
                            "callback_data": f"a:{configure_action.token}",
                        }
                    ],
                    [
                        {
                            "text": f"Switch to {other_provider_name}…",
                            "callback_data": (
                                f"a:{switch_provider_action.token}"
                            ),
                        }
                    ],
                ]
            }
    if agent is None:
        send_message("This Telegram surface has no managed agent.")
        return
    with DurableStore(Path(database_path)) as store:
        # Durable project identity, not a directory basename.
        project_name = store.agent_speaker_header(agent.agent_id)
    workspace_lines = ""
    if agent.project_path:
        workspace_name = Path(agent.project_path).name
        workspace_lines = f"\nWorkspace: {workspace_name}"
        workdir = agent.working_directory or agent.project_path
        if workdir != agent.project_path:
            try:
                relative = Path(workdir).relative_to(agent.project_path)
                workspace_lines += f"\nWorking directory: {relative}"
            except ValueError:
                workspace_lines += (
                    f"\nWorking directory: {Path(workdir).name}"
                )
        workspace_lines += (
            "\nGit: repository detected"
            if agent.git_repository_root is not None
            else "\nGit: not required"
        )
    session = "not started" if not agent.provider_session_id else "persisted"
    console_state = console.state if console is not None else "stopped"
    model_name, effort_name = provider_defaults.describe_provider_config(
        agent.provider,
        agent.provider_config,
        agent.project_path,
    )
    usage_line = ""
    context_line = ""
    if usage is not None:
        input_tokens = int(usage.get("input_tokens", 0))
        cached_tokens = int(
            usage.get(
                "cached_input_tokens",
                usage.get("cache_read_input_tokens", 0),
            )
        )
        output_tokens = int(usage.get("output_tokens", 0))
        usage_line = (
            "\nLast turn: "
            f"{input_tokens:,} input ({cached_tokens:,} cached) · "
            f"{output_tokens:,} output"
        )
        context_summary = context_usage_summary(usage)
        if context_summary is not None:
            context_line = f"\nContext: {context_summary}"
    send_message(
        "Managed agent\n\n"
        f"Name: {agent.hierarchical_name}\n"
        f"Role: {agent.role}\n"
        f"Provider: {agent.provider}\n"
        f"Model: {model_name}\n"
        f"Effort: {effort_name}\n"
        f"Project: {project_name}"
        f"{workspace_lines}\n"
        f"State: {agent.lifecycle_state}\n"
        f"Session: {session}\n"
        f"Console: {console_state}"
        f"{context_line}"
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


def send_group_setup_card() -> None:
    """Hand over the one-tap link that adds this bot to a project group."""
    link = bridge.group_setup_link({"bot_username": bot_username()})
    send_message(
        "Add me to a new project group:\n\n"
        "1. Create a private Telegram group — any name.\n"
        "2. Turn on Topics in the group's settings.\n"
        "3. Tap the button below and pick that group. Telegram adds me and "
        "asks you to grant Change group info, Delete messages, and Manage "
        "topics in the same step — no separate promotion.\n\n"
        "Then send anything in the group and I will ask which folder it works "
        "in. A bot cannot create the group or enable Topics itself; those two "
        "steps are yours.\n\n"
        f"{telegram_help.HELP_HINT}",
        reply_markup={
            "inline_keyboard": [
                [{"text": "Add me to a group", "url": link}]
            ]
        },
    )


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
            "Send a message in this topic to start its provider session.\n\n"
            f"{telegram_help.HELP_HINT}"
        )
    else:
        send_message(
            f"✅ {agent.hierarchical_name} is already attached to this topic."
        )


def enqueue_agent_input(
    agent_id: str,
    text: str,
    receipt_text: Optional[str] = None,
) -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    if not database_path or not job_id:
        raise StoreError("Managed agent input requires the durable controller.")
    with DurableStore(Path(database_path)) as store:
        agent = store.resolve_agent(agent_id)
        if agent is None or agent.role not in {"project", "worker"}:
            raise StoreError("Managed agent route is no longer valid.")
        chat_id, thread_id = surface_coordinates()
        speaker = html.escape(
            store.agent_surface_header(agent.agent_id, chat_id, thread_id)
        )
        provider_summary = provider_defaults.provider_turn_summary(
            agent.provider,
            agent.provider_config,
            agent.project_path,
        )
        context_snapshot = store.agent_context_snapshot(agent.agent_id)
        metadata_line = f"\n⚙️ <b>{html.escape(provider_summary)}</b>"
        context_line = (
            "\n📊 <b>Context before this turn:</b> "
            f"{context_snapshot}"
            if context_snapshot is not None
            else ""
        )
        receipt = (
            f"📨 <b>Queued{f' for {speaker}' if speaker else ''}</b>"
            f"{metadata_line}"
            f"{context_line}"
        )
        store.enqueue_agent_message_with_receipt(
            agent_id=agent.agent_id,
            source_inbox_job_id=int(job_id),
            input_text=text,
            chat_id=chat_id,
            message_thread_id=thread_id,
            receipt_text=receipt_text or receipt,
            receipt_parse_mode="HTML",
            authorized_user_id=int(os.environ["TELEGRAM_FROM_ID"]),
        )


def enqueue_router_input(
    text: str,
    replied_message_id: Optional[int] = None,
    receipt_text: Optional[str] = None,
) -> None:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    if not database_path or not job_id:
        raise StoreError("Main-router input requires the durable controller.")
    chat_id, thread_id = surface_coordinates()
    with DurableStore(Path(database_path)) as store:
        binding = store.resolve_surface_binding(chat_id, thread_id)
        if binding is None and replied_message_id is None:
            binding = store.ensure_surface_binding(
                chat_id=chat_id,
                message_thread_id=thread_id,
                surface_type="control",
                display_name=surface_display_name(),
                target_type="controller",
                target_id="control",
            )
        if replied_message_id is None and (
            binding is None
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
            receipt_text=receipt_text or "🧭 <b>Control is routing…</b>",
            receipt_parse_mode="HTML",
            replied_message_id=replied_message_id,
        )


def proposed_forum_setup(text: str) -> Optional[dict]:
    """Recognize an explicit first-message forum setup with a local path."""
    if not re.search(r"\b(?:set\s*up|bind|workspace)\b", text, re.IGNORECASE):
        return None
    lowered = text.casefold()
    mentions_codex = bool(re.search(r"\bcodex\b", lowered))
    mentions_claude = bool(re.search(r"\bclaude\b", lowered))
    if mentions_codex and mentions_claude:
        return None
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    for token in tokens:
        raw_path = token.strip("`'\"()[]{}.,;:!?")
        if not (raw_path.startswith("/") or raw_path.startswith("~/")):
            continue
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            continue
        roots = discovery.load_discovery_roots()
        if not discovery.within_roots(str(candidate), roots):
            continue
        real = os.path.realpath(candidate)
        if not Path(real).is_dir():
            continue
        git_root = discovery.exact_git_root(real)
        workspace_root, workdir, git_root = discovery.validate_agent_workspace(
            real,
            real,
            git_root,
        )
        return {
            "project_path": workspace_root,
            "working_directory": workdir,
            "git_repository_root": git_root,
            "provider": "claude" if mentions_claude else "codex",
            "provider_config": {},
        }
    return None


def forum_is_authorized_or_prompt(text: Optional[str] = None) -> bool:
    """Require one explicit owner confirmation before a forum reaches Control."""
    if os.environ.get("TELEGRAM_CHAT_TYPE") != "supergroup":
        return True
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    if not thread_id_text:
        raise StoreError("Private forum input requires a Telegram topic.")
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    chat_id_text = os.environ.get("TELEGRAM_CHAT_ID")
    user_id_text = os.environ.get("TELEGRAM_FROM_ID")
    if not all((database_path, job_id, chat_id_text, user_id_text)):
        raise StoreError(
            "Private forum authorization requires the durable controller."
        )
    chat_id = int(chat_id_text)
    thread_id = int(thread_id_text)
    display_name = (
        os.environ.get("TELEGRAM_CHAT_TITLE") or f"Forum {chat_id}"
    ).strip()
    if not display_name or len(display_name) > 128:
        raise StoreError("Private forum display name is invalid.")
    with DurableStore(Path(database_path)) as store:
        forum_binding = store.resolve_surface_binding(chat_id, None)
        if forum_binding is not None:
            if (
                forum_binding.target_type != "controller"
                or forum_binding.target_id != "control"
                or forum_binding.state != "active"
            ):
                raise StoreError("Private forum authorization is invalid.")
            return True
        setup = proposed_forum_setup(text) if text else None
        if setup is not None:
            payload = {
                "chat_id": chat_id,
                "display_name": display_name,
                **setup,
            }
            confirm = store.create_callback_action(
                operation_id=f"inbox:{job_id}:authorize-bind-forum",
                action_type="authorize_bind_forum",
                payload=payload,
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=int(user_id_text),
                one_time=True,
                ttl_seconds=60 * 60,
            )
            cancel = store.create_callback_action(
                operation_id=f"inbox:{job_id}:authorize-bind-forum-cancel",
                action_type="authorize_bind_forum_cancel",
                payload={
                    "chat_id": chat_id,
                    "display_name": display_name,
                },
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=int(user_id_text),
                one_time=True,
                ttl_seconds=60 * 60,
            )
            send_message(
                f"Set up {display_name} for this workspace?\n\n"
                f"Workspace: {setup['project_path']}\n"
                f"Provider: {setup['provider']}\n\n"
                "This authorizes the private forum and binds its topics to "
                "that workspace. Nothing changes until you confirm.\n\n"
                f"{telegram_help.HELP_HINT}",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {
                                "text": "Authorize and bind",
                                "callback_data": f"a:{confirm.token}",
                            }
                        ],
                        [
                            {
                                "text": "Cancel",
                                "callback_data": f"a:{cancel.token}",
                            }
                        ],
                    ]
                },
            )
            return False
        action = store.create_callback_action(
            operation_id=f"inbox:{job_id}:authorize-forum",
            action_type="authorize_forum",
            payload={
                "chat_id": chat_id,
                "display_name": display_name,
            },
            chat_id=chat_id,
            message_thread_id=thread_id,
            authorized_user_id=int(user_id_text),
            one_time=True,
            ttl_seconds=60 * 60,
        )
    send_message(
        f"Authorize this private forum for {bot_label()}?\n\n"
        f"Forum: {display_name}\n\n"
        f"Only your paired Telegram account will be accepted. Add {bot_label()} "
        "as a forum administrator so ordinary text and voice messages reach "
        "the controller.\n\n"
        f"{WORKSPACE_QUESTION}\n\n"
        f"{telegram_help.HELP_HINT}",
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": "Authorize forum",
                        "callback_data": f"a:{action.token}",
                    }
                ]
            ]
        },
    )
    return False


def ensure_bound_forum_subject():
    """Turn a topic in a bound private forum into a durable conversation."""
    if os.environ.get("TELEGRAM_CHAT_TYPE") != "supergroup":
        return None
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    chat_id_text = os.environ.get("TELEGRAM_CHAT_ID")
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    if not database_path or not chat_id_text or not thread_id_text:
        raise StoreError(
            "Private forum subjects require the durable controller."
        )
    chat_id = int(chat_id_text)
    thread_id = int(thread_id_text)
    with DurableStore(Path(database_path)) as store:
        if store.resolve_forum_workspace(chat_id) is None:
            # Until the forum is bound, its topics remain Control surfaces so
            # the main router can complete the conversational binding flow.
            return None
        existing = store.resolve_forum_subject(chat_id, thread_id)
        if existing is not None:
            return existing, False
        binding = store.resolve_surface_binding(chat_id, thread_id)
        if binding is not None and binding.target_type == "agent":
            # Schema v19 deliberately does not rewrite existing project-agent
            # topics. They keep their current durable agent/session route;
            # only unclaimed Control topics are provisioned as subjects.
            return None
        topic_name = os.environ.get("TELEGRAM_TOPIC_NAME", "").strip()
        display_name = (
            binding.display_name
            if binding is not None
            else topic_name or f"Topic {thread_id}"
        )
        return store.ensure_forum_subject(
            chat_id=chat_id,
            message_thread_id=thread_id,
            display_name=display_name,
        )


PENDING_REQUEST_LIMIT = 4_000


def carryable_pending_request(text: Optional[str]) -> Optional[str]:
    """Keep a request that setup interrupted so the user need not resend it."""
    if text is None:
        return None
    pending = text.strip()
    if not pending or len(pending) > PENDING_REQUEST_LIMIT:
        return None
    return pending


def pending_request_from_payload(payload: dict) -> Optional[str]:
    raw = payload.get("pending_request")
    return carryable_pending_request(raw) if isinstance(raw, str) else None


def send_topic_intro(display_name: str, started: bool) -> None:
    """Open the topic with its standing header.

    Commands are reachable from Telegram's registered command menu, so this
    message exists for state rather than navigation: later turns edit it in
    place so its model, effort, and context stay current.
    """
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    binding = current_surface_binding()
    if not database_path or binding is None or binding.target_type != "agent":
        raise StoreError("A topic intro requires its managed agent.")
    with DurableStore(Path(database_path)) as store:
        text = store.topic_intro_text(binding.target_id, display_name, started)
    send_message(text, card={"kind": "topic_intro", "mode": "record"})


def start_forum_subject_turn(
    display_name: str,
    pending_request: Optional[str],
) -> None:
    """Introduce a newly configured topic, and run any request it held."""
    binding = current_surface_binding()
    started = (
        pending_request is not None
        and binding is not None
        and binding.target_type == "agent"
    )
    send_topic_intro(display_name, started)
    if started:
        enqueue_agent_input(binding.target_id, pending_request)


def forum_subject_setup_pending() -> bool:
    """Report whether this bound-forum topic still needs its agent confirmed."""
    if os.environ.get("TELEGRAM_CHAT_TYPE") != "supergroup":
        return False
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    chat_id_text = os.environ.get("TELEGRAM_CHAT_ID")
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    if not database_path or not chat_id_text or not thread_id_text:
        return False
    chat_id = int(chat_id_text)
    thread_id = int(thread_id_text)
    with DurableStore(Path(database_path)) as store:
        if store.resolve_forum_workspace(chat_id) is None:
            return False
        if store.resolve_forum_subject(chat_id, thread_id) is not None:
            return False
        binding = store.resolve_surface_binding(chat_id, thread_id)
        return not (binding is not None and binding.target_type == "agent")


def forum_workspace_path() -> Optional[str]:
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    chat_id_text = os.environ.get("TELEGRAM_CHAT_ID")
    if not database_path or not chat_id_text:
        return None
    with DurableStore(Path(database_path)) as store:
        workspace = store.resolve_forum_workspace(int(chat_id_text))
    return workspace.project_path if workspace is not None else None


def prompt_forum_subject_provider_selection(
    request_was_already_sent: bool = False,
    pending_request: Optional[str] = None,
) -> bool:
    """Confirm a topic's agent before its first turn, using forum defaults.

    The forum binding already recorded the provider and any model/effort
    defaults for its subjects, so asking all three again is redundant. The
    normal path is one button that starts the recorded configuration; the
    per-topic provider, model, and effort menus stay available behind
    **Choose a different agent…** for a topic that should differ from its
    group.
    """
    if os.environ.get("TELEGRAM_CHAT_TYPE") != "supergroup":
        return False
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    chat_id_text = os.environ.get("TELEGRAM_CHAT_ID")
    thread_id_text = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "")
    user_id_text = os.environ.get("TELEGRAM_FROM_ID")
    if not all(
        (
            database_path,
            job_id,
            chat_id_text,
            thread_id_text,
            user_id_text,
        )
    ):
        raise StoreError(
            "Forum subject provider selection requires the durable controller."
        )
    chat_id = int(chat_id_text)
    thread_id = int(thread_id_text)
    display_name = " ".join(surface_display_name().strip().split())
    if not display_name or len(display_name) > 128:
        raise StoreError("Forum topic display name is invalid.")

    with DurableStore(Path(database_path)) as store:
        workspace = store.resolve_forum_workspace(chat_id)
        if workspace is None:
            return False
        subject = store.resolve_forum_subject(chat_id, thread_id)
        if subject is not None:
            return False
        binding = store.resolve_surface_binding(chat_id, thread_id)
        if binding is not None and binding.target_type == "agent":
            return False
        pending = carryable_pending_request(pending_request)
        payload = {
            "chat_id": chat_id,
            "message_thread_id": thread_id,
            "display_name": display_name,
        }
        if pending is not None:
            payload["pending_request"] = pending
        start = store.create_callback_action(
            operation_id=f"inbox:{job_id}:forum-subject-start",
            action_type="forum_subject_start",
            payload={
                **payload,
                "provider": workspace.provider,
                "provider_config": workspace.provider_config,
            },
            chat_id=chat_id,
            message_thread_id=thread_id,
            authorized_user_id=int(user_id_text),
            one_time=True,
            ttl_seconds=24 * 60 * 60,
        )
        customize = store.create_callback_action(
            operation_id=f"inbox:{job_id}:forum-subject-customize",
            action_type="forum_subject_customize",
            payload=payload,
            chat_id=chat_id,
            message_thread_id=thread_id,
            authorized_user_id=int(user_id_text),
            one_time=True,
            ttl_seconds=24 * 60 * 60,
        )

    provider_name = "Claude" if workspace.provider == "claude" else "Codex"
    model_name, effort_name = provider_defaults.describe_provider_config(
        workspace.provider,
        workspace.provider_config,
        workspace.project_path,
    )
    if pending is not None:
        instruction = "Your message is saved and runs as its first turn."
    elif request_was_already_sent:
        instruction = "Send your request again once this topic has its agent."
    else:
        instruction = "Your first message will start its session."
    send_message(
        f"Start “{display_name}” with {provider_name}?\n\n"
        f"Model: {model_name}\n"
        f"Effort: {effort_name}\n\n"
        f"{instruction}\n\n"
        f"{telegram_help.HELP_HINT}",
        reply_markup={
            "inline_keyboard": [
                [
                    {
                        "text": f"▶️ Start {provider_name}",
                        "callback_data": f"a:{start.token}",
                    }
                ],
                [
                    {
                        "text": "Choose a different agent…",
                        "callback_data": f"a:{customize.token}",
                    }
                ],
            ]
        },
    )
    return True


def option_button_rows(actions, width: int = 3) -> list[list[dict]]:
    buttons = [
        {
            "text": label,
            "callback_data": f"a:{action.token}",
        }
        for label, action in actions
    ]
    return [
        buttons[index : index + width]
        for index in range(0, len(buttons), width)
    ]


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

    if action.action_type == "help_topic":
        topic_slug = str(action.payload.get("topic", ""))
        menu_id = str(action.payload.get("menu_id", ""))
        try:
            page_text = telegram_help.page_text(topic_slug)
        except ValueError:
            raise StoreError("Stored help topic is invalid.") from None
        if not menu_id or len(menu_id) > 80:
            raise StoreError("Stored help menu identity is invalid.")
        with DurableStore(Path(database_path)) as store:
            reply_markup = help_reply_markup(
                store,
                menu_id=menu_id,
                chat_id=chat_id,
                thread_id=thread_id,
                user_id=user_id,
                current_slug=topic_slug,
            )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Help topic opened.",
                },
                "callback-answer",
            )
        deliver_api_call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": int(os.environ["TELEGRAM_MESSAGE_ID"]),
                "text": speaker_labeled_text(page_text),
                "reply_markup": reply_markup,
            },
            f"help-page:{topic_slug}",
        )
        return

    if action.action_type == "agent_topic_teardown_cancel":
        confirm_operation_id = str(
            action.payload.get("confirm_operation_id")
            or action.operation_id.replace(
                "topic-teardown-cancel",
                "topic-teardown-confirm",
            )
        )
        with DurableStore(Path(database_path)) as store:
            store.retire_callback_action_operation(
                confirm_operation_id,
                "agent_topic_teardown_confirm",
            )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Topic teardown cancelled.",
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
            "topic-teardown-cancel-clear",
        )
        return

    if action.action_type == "agent_topic_teardown_confirm":
        agent_id = str(action.payload.get("agent_id", ""))
        binding_id = int(action.payload.get("binding_id", 0))
        target_chat_id = int(action.payload.get("chat_id", 0))
        target_thread_id = int(action.payload.get("message_thread_id", 0))
        if (
            not agent_id
            or binding_id <= 0
            or target_chat_id != chat_id
            or target_thread_id != int(thread_id or 0)
        ):
            raise StoreError("Stored topic teardown target is invalid.")
        try:
            with DurableStore(Path(database_path)) as store:
                bound_agent = store.resolve_agent_for_surface(
                    target_chat_id,
                    target_thread_id,
                )
                if bound_agent is None or bound_agent.agent_id != agent_id:
                    raise StoreError("Managed topic changed before teardown.")
                console = tmux_console.reconcile_agent_console(store, agent_id)
                if console is not None and console.state in {
                    "starting",
                    "running",
                }:
                    tmux_console.close_agent_console(store, bound_agent)
                store.teardown_managed_topic(
                    binding_id=binding_id,
                    agent_id=agent_id,
                    delete_operation_id=(
                        f"{action.operation_id}:delete-forum-topic"
                    ),
                )
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
                    "text": "Topic teardown queued.",
                },
                "callback-answer",
            )
        return

    if action.action_type in {
        "forum_subject_start",
        "forum_subject_customize",
    }:
        target_chat_id = int(action.payload.get("chat_id", 0))
        target_thread_id = int(action.payload.get("message_thread_id", 0))
        display_name = str(action.payload.get("display_name", "")).strip()
        if (
            target_chat_id != chat_id
            or target_thread_id != thread_id
            or not display_name
            or len(display_name) > 128
        ):
            raise StoreError("Stored forum topic setup is invalid.")
        pending_request = pending_request_from_payload(action.payload)
        clear_operation = (
            "forum-subject-start-clear"
            if action.action_type == "forum_subject_start"
            else "forum-subject-customize-clear"
        )
        if action.action_type == "forum_subject_customize":
            with DurableStore(Path(database_path)) as store:
                provider_actions = []
                for provider in ("codex", "claude"):
                    payload = {
                        "chat_id": chat_id,
                        "message_thread_id": target_thread_id,
                        "display_name": display_name,
                        "provider": provider,
                    }
                    if pending_request is not None:
                        payload["pending_request"] = pending_request
                    provider_actions.append(
                        (
                            "Claude" if provider == "claude" else "Codex",
                            store.create_callback_action(
                                operation_id=(
                                    f"callback:{update['update_id']}:"
                                    f"forum-subject-provider:{provider}"
                                ),
                                action_type="forum_subject_provider_select",
                                payload=payload,
                                chat_id=chat_id,
                                message_thread_id=target_thread_id,
                                authorized_user_id=user_id,
                                one_time=True,
                                ttl_seconds=24 * 60 * 60,
                            ),
                        )
                    )
                store.expire_forum_subject_setup_actions(
                    chat_id,
                    target_thread_id,
                    action_type="forum_subject_start",
                )
            if callback_query_id:
                deliver_api_call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_query_id,
                        "text": "Choose an agent.",
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
                clear_operation,
            )
            send_message(
                f"Choose an agent for “{display_name}”.",
                reply_markup={
                    "inline_keyboard": option_button_rows(provider_actions, 2),
                },
            )
            return

        # The forum's own record is the authority for a default start, so a
        # stored payload can never widen the provider it was issued against.
        try:
            with DurableStore(Path(database_path)) as store:
                workspace = store.resolve_forum_workspace(chat_id)
                if workspace is None:
                    raise StoreError(
                        "This forum is no longer bound to a workspace."
                    )
                subject, _ = store.ensure_forum_subject(
                    chat_id=chat_id,
                    message_thread_id=target_thread_id,
                    display_name=display_name,
                    provider=workspace.provider,
                    provider_config=workspace.provider_config,
                )
                agent = store.resolve_agent(subject.agent_id)
                if agent is None or agent.provider != workspace.provider:
                    raise StoreError(
                        "The selected forum agent could not be verified."
                    )
                store.expire_forum_subject_setup_actions(
                    chat_id,
                    target_thread_id,
                )
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
                    "text": (
                        "Claude started."
                        if workspace.provider == "claude"
                        else "Codex started."
                    ),
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
            clear_operation,
        )
        start_forum_subject_turn(display_name, pending_request)
        return

    if action.action_type == "forum_subject_provider_select":
        target_chat_id = int(action.payload.get("chat_id", 0))
        target_thread_id = int(action.payload.get("message_thread_id", 0))
        display_name = str(action.payload.get("display_name", "")).strip()
        provider = str(action.payload.get("provider", ""))
        if (
            target_chat_id != chat_id
            or target_thread_id != thread_id
            or provider not in {"codex", "claude"}
            or not display_name
            or len(display_name) > 128
        ):
            raise StoreError("Stored forum agent selection is invalid.")
        provider_name = "Claude" if provider == "claude" else "Codex"
        provider_options = provider_adapters.configuration_options(provider)
        pending_request = pending_request_from_payload(action.payload)
        with DurableStore(Path(database_path)) as store:
            model_actions = []
            for label, model in provider_options.models:
                model_action = store.create_callback_action(
                    operation_id=(
                        f"callback:{update['update_id']}:"
                        f"forum-subject-model:{model or 'default'}"
                    ),
                    action_type="forum_subject_model_select",
                    payload={
                        "chat_id": chat_id,
                        "message_thread_id": target_thread_id,
                        "display_name": display_name,
                        "provider": provider,
                        "model": model,
                        **(
                            {"pending_request": pending_request}
                            if pending_request is not None
                            else {}
                        ),
                    },
                    chat_id=chat_id,
                    message_thread_id=target_thread_id,
                    authorized_user_id=user_id,
                    one_time=True,
                    ttl_seconds=24 * 60 * 60,
                )
                model_actions.append((label, model_action))
            store.expire_forum_subject_setup_actions(
                chat_id,
                target_thread_id,
                action_type="forum_subject_provider_select",
            )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": f"{provider_name} selected. Choose a model.",
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
            "forum-subject-provider-clear",
        )
        send_message(
            f"{provider_name} selected. Choose a model.",
            reply_markup={
                "inline_keyboard": option_button_rows(model_actions),
            },
        )
        return

    if action.action_type == "forum_subject_model_select":
        target_chat_id = int(action.payload.get("chat_id", 0))
        target_thread_id = int(action.payload.get("message_thread_id", 0))
        display_name = str(action.payload.get("display_name", "")).strip()
        provider = str(action.payload.get("provider", ""))
        model = action.payload.get("model")
        allowed_models = (
            {
                value
                for _, value in provider_adapters.configuration_options(
                    provider
                ).models
            }
            if provider in {"codex", "claude"}
            else set()
        )
        if (
            target_chat_id != chat_id
            or target_thread_id != thread_id
            or not display_name
            or len(display_name) > 128
            or model not in allowed_models
        ):
            raise StoreError("Stored forum model selection is invalid.")
        provider_options = provider_adapters.configuration_options(provider)
        pending_request = pending_request_from_payload(action.payload)
        with DurableStore(Path(database_path)) as store:
            effort_actions = []
            for label, effort in provider_options.efforts:
                effort_action = store.create_callback_action(
                    operation_id=(
                        f"callback:{update['update_id']}:"
                        f"forum-subject-effort:{effort or 'default'}"
                    ),
                    action_type="forum_subject_effort_select",
                    payload={
                        "chat_id": chat_id,
                        "message_thread_id": target_thread_id,
                        "display_name": display_name,
                        "provider": provider,
                        "model": model,
                        "effort": effort,
                        **(
                            {"pending_request": pending_request}
                            if pending_request is not None
                            else {}
                        ),
                    },
                    chat_id=chat_id,
                    message_thread_id=target_thread_id,
                    authorized_user_id=user_id,
                    one_time=True,
                    ttl_seconds=24 * 60 * 60,
                )
                effort_actions.append((label, effort_action))
            forum_workspace = store.resolve_forum_workspace(chat_id)
            store.expire_forum_subject_setup_actions(
                chat_id,
                target_thread_id,
                action_type="forum_subject_model_select",
            )
        model_name, _ = provider_defaults.describe_provider_config(
            provider,
            {"model": str(model)} if model is not None else {},
            (
                forum_workspace.project_path
                if forum_workspace is not None
                else None
            ),
        )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Model selected. Choose effort.",
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
            "forum-subject-model-clear",
        )
        send_message(
            f"Model: {model_name}\n\nChoose effort.",
            reply_markup={
                "inline_keyboard": option_button_rows(effort_actions),
            },
        )
        return

    if action.action_type == "forum_subject_effort_select":
        target_chat_id = int(action.payload.get("chat_id", 0))
        target_thread_id = int(action.payload.get("message_thread_id", 0))
        display_name = str(action.payload.get("display_name", "")).strip()
        provider = str(action.payload.get("provider", ""))
        model = action.payload.get("model")
        effort = action.payload.get("effort")
        allowed_models = (
            {
                value
                for _, value in provider_adapters.configuration_options(
                    provider
                ).models
            }
            if provider in {"codex", "claude"}
            else set()
        )
        allowed_efforts = (
            {
                value
                for _, value in provider_adapters.configuration_options(
                    provider
                ).efforts
            }
            if provider in {"codex", "claude"}
            else set()
        )
        if (
            target_chat_id != chat_id
            or target_thread_id != thread_id
            or not display_name
            or len(display_name) > 128
            or model not in allowed_models
            or effort not in allowed_efforts
        ):
            raise StoreError("Stored forum effort selection is invalid.")
        provider_config = {}
        if model is not None:
            provider_config["model"] = str(model)
        if effort is not None:
            provider_config["effort"] = str(effort)
        try:
            with DurableStore(Path(database_path)) as store:
                subject, _ = store.ensure_forum_subject(
                    chat_id=chat_id,
                    message_thread_id=target_thread_id,
                    display_name=display_name,
                    provider=provider,
                    provider_config=provider_config,
                )
                agent = store.resolve_agent(subject.agent_id)
                if (
                    agent is None
                    or agent.provider != provider
                    or agent.provider_config != provider_config
                ):
                    raise StoreError(
                        "The selected forum agent could not be verified."
                    )
                store.expire_forum_subject_setup_actions(
                    chat_id,
                    target_thread_id,
                )
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
        provider_name = "Claude" if provider == "claude" else "Codex"
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": f"{provider_name} configured.",
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
            "forum-subject-effort-clear",
        )
        start_forum_subject_turn(
            display_name,
            pending_request_from_payload(action.payload),
        )
        return

    if action.action_type == "authorize_forum":
        target_chat_id = int(action.payload.get("chat_id", 0))
        display_name = str(action.payload.get("display_name", "")).strip()
        if (
            target_chat_id != chat_id
            or not display_name
            or len(display_name) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in display_name
            )
        ):
            raise StoreError("Stored private-forum authorization is invalid.")
        with DurableStore(Path(database_path)) as store:
            store.ensure_surface_binding(
                chat_id=chat_id,
                message_thread_id=None,
                surface_type="control",
                display_name=display_name,
                target_type="controller",
                target_id="control",
            )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": f"{display_name} authorized.",
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
            "forum-authorization-clear",
        )
        with DurableStore(Path(database_path)) as store:
            already_bound = store.resolve_forum_workspace(chat_id) is not None
        send_message(
            f"✅ {display_name} is authorized.\n\n"
            + (
                "Send your text or voice request in this topic again."
                if already_bound
                else WORKSPACE_QUESTION
            )
            + f"\n\n{telegram_help.HELP_HINT}"
        )
        return
    if action.action_type in {
        "authorize_bind_forum",
        "authorize_bind_forum_cancel",
    }:
        target_chat_id = int(action.payload.get("chat_id", 0))
        display_name = str(action.payload.get("display_name", "")).strip()
        if (
            target_chat_id != chat_id
            or target_chat_id >= 0
            or not display_name
            or len(display_name) > 128
        ):
            raise StoreError("Stored forum setup action is invalid.")
        if action.action_type == "authorize_bind_forum_cancel":
            if callback_query_id:
                deliver_api_call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_query_id,
                        "text": "Cancelled.",
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
                "forum-setup-clear",
            )
            send_message("Cancelled. The forum was not authorized or bound.")
            return
        required = {
            "project_path",
            "working_directory",
            "git_repository_root",
            "provider",
            "provider_config",
        }
        if not required.issubset(action.payload):
            raise StoreError("Stored forum setup plan is invalid.")
        provider_config = action.payload["provider_config"]
        if not isinstance(provider_config, dict):
            raise StoreError("Stored forum setup configuration is invalid.")
        workspace_root, working_directory, git_repository_root = (
            discovery.validate_agent_workspace(
                str(action.payload["project_path"]),
                str(action.payload["working_directory"]),
                (
                    str(action.payload["git_repository_root"])
                    if action.payload["git_repository_root"] is not None
                    else None
                ),
            )
        )
        if (
            workspace_root != str(action.payload["project_path"])
            or working_directory != str(action.payload["working_directory"])
            or git_repository_root != action.payload["git_repository_root"]
        ):
            raise StoreError(
                "The forum workspace changed before confirmation."
            )
        with DurableStore(Path(database_path)) as store:
            workspace, _created = store.authorize_and_bind_forum_workspace(
                chat_id=target_chat_id,
                display_name=display_name,
                project_path=workspace_root,
                working_directory=working_directory,
                git_repository_root=git_repository_root,
                provider=str(action.payload["provider"]),
                provider_config=provider_config,
            )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": f"{display_name} is ready.",
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
            "forum-setup-clear",
        )
        send_message(
            f"✅ {workspace.display_name} is authorized and bound.\n\n"
            "Create or open any topic and send your request. Its subject "
            "agent will be created automatically.\n\n"
            f"{telegram_help.HELP_HINT}"
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
    if action.action_type == "agent_turn_stop":
        mailbox_id = int(action.payload.get("mailbox_id", 0))
        agent_id = str(action.payload.get("agent_id", ""))
        source_job_id = int(os.environ["TELEGRAM_CONTROL_JOB_ID"])
        if mailbox_id <= 0 or not agent_id:
            raise StoreError("Stored Stop action is invalid.")
        with DurableStore(Path(database_path)) as store:
            outcome = store.request_agent_turn_cancel(
                mailbox_id=mailbox_id,
                agent_id=agent_id,
                source_inbox_job_id=source_job_id,
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
        answer = {
            "stopping": "Stopping the active Codex turn…",
            "starting": "Stop queued while Codex starts…",
            "cancelled": "Cancelled before it started.",
            "finished": "That turn already finished.",
        }[outcome]
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": answer,
                },
                "callback-answer",
            )
        return
    if action.action_type == "agent_voice_reply":
        mailbox_id = int(action.payload.get("mailbox_id", 0))
        agent_id = str(action.payload.get("agent_id", ""))
        if mailbox_id <= 0 or not agent_id:
            raise StoreError("Stored voice-response action is invalid.")
        try:
            with DurableStore(Path(database_path)) as store:
                response_text, _speaker = store.resolve_agent_voice_text(
                    mailbox_id,
                    agent_id,
                )
                protected_voice_paths = store.pending_voice_file_paths()
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
                    "text": "Generating via Microsoft TTS…",
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
            "voice-button-clear",
        )
        source_job_id = int(os.environ["TELEGRAM_CONTROL_JOB_ID"])
        voice_path = None
        try:
            voice_path = voice_responses.synthesize_voice(
                response_text,
                f"agent-{mailbox_id}-request-{source_job_id}",
                protected_paths=protected_voice_paths,
            )
            with DurableStore(Path(database_path)) as store:
                store.enqueue_agent_voice_response(
                    mailbox_id=mailbox_id,
                    agent_id=agent_id,
                    source_inbox_job_id=source_job_id,
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    authorized_user_id=user_id,
                    voice_file_path=str(voice_path),
                )
        except (voice_responses.VoiceResponseError, StoreError):
            if voice_path is not None:
                voice_responses.remove_voice_file(str(voice_path))
            with DurableStore(Path(database_path)) as store:
                store.enqueue_agent_voice_failure(
                    mailbox_id=mailbox_id,
                    agent_id=agent_id,
                    source_inbox_job_id=source_job_id,
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    authorized_user_id=user_id,
                )
        return
    if action.action_type == "agent_choice":
        target_agent_id = str(action.payload.get("agent_id", ""))
        question = str(action.payload.get("question", ""))
        choice = str(action.payload.get("choice", ""))
        prompt_key = str(action.payload.get("prompt_key", ""))
        prompt_mailbox_id = int(action.payload.get("mailbox_id", 0))
        binding = current_surface_binding()
        if (
            not target_agent_id
            or not question
            or not choice
            or not prompt_key
            or binding is None
            or binding.target_type != "agent"
            or binding.target_id != target_agent_id
        ):
            raise StoreError("Stored agent question is no longer valid.")
        with DurableStore(Path(database_path)) as store:
            answer = store.resolve_agent_choice(
                prompt_mailbox_id,
                prompt_key,
                question,
                choice,
            )
        enqueue_agent_input(target_agent_id, answer)
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
            "agent-choice-clear",
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
                receipt_text="🧭 <b>Control is routing…</b>",
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
        "router_topic_rename_confirm",
        "router_topic_rename_cancel",
    }:
        router_mailbox_id = int(action.payload.get("router_mailbox_id", 0))
        with DurableStore(Path(database_path)) as store:
            store.expire_router_topic_rename_actions(router_mailbox_id)
        if action.action_type == "router_topic_rename_cancel":
            if callback_query_id:
                deliver_api_call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_query_id,
                        "text": "Cancelled.",
                    },
                    "callback-answer",
                )
            send_message("🎛 Control\n\nCancelled. The Telegram topic was not renamed.")
            return

        required = {
            "binding_id",
            "chat_id",
            "message_thread_id",
            "old_name",
            "new_name",
        }
        if not required.issubset(action.payload):
            raise StoreError("Stored topic-rename plan is invalid.")
        binding_id = int(action.payload["binding_id"])
        target_chat_id = int(action.payload["chat_id"])
        target_thread_id = int(action.payload["message_thread_id"])
        old_name = str(action.payload["old_name"])
        new_name = str(action.payload["new_name"]).strip()
        if (
            target_chat_id != chat_id
            or binding_id <= 0
            or target_thread_id <= 0
            or not old_name
            or not new_name
            or len(new_name) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in new_name
            )
        ):
            raise StoreError("Stored topic-rename plan is invalid.")
        with DurableStore(Path(database_path)) as store:
            mutation = store.prepare_telegram_mutation(
                action.operation_id,
                "topic_rename",
                action.payload,
            )
            binding = store.resolve_surface_binding_by_id(binding_id)
        if mutation.state == "applied":
            send_message(
                "🎛 Control\n\n"
                f"The topic rename from “{old_name}” to “{new_name}” "
                "was already completed."
            )
            return
        binding_identity_matches = (
            binding is not None
            and binding.chat_id == target_chat_id
            and binding.message_thread_id == target_thread_id
        )
        if not binding_identity_matches:
            raise StoreError(
                "Managed topic changed before the rename was confirmed."
            )

        if mutation.state in {
            "external_in_flight",
            "reconciliation_required",
        }:
            # A durable local rename proves that the result was applied before
            # a prior handler stopped. Otherwise Telegram provides no safe
            # idempotency key or topic-read method, so never guess by issuing
            # editForumTopic a second time.
            if binding.display_name == new_name:
                with DurableStore(Path(database_path)) as store:
                    store.record_telegram_mutation_result(
                        action.operation_id,
                        {"telegram_result": True, "reconciled": "local_binding"},
                        reconciled=True,
                    )
                    store.complete_telegram_mutation(action.operation_id)
                send_message(
                    "🎛 Control\n\n"
                    f"Recovered the completed rename to “{new_name}” "
                    "without sending it to Telegram again."
                )
            else:
                with DurableStore(Path(database_path)) as store:
                    store.require_telegram_mutation_reconciliation(
                        action.operation_id,
                        "The prior Telegram rename call has no durable result.",
                    )
                send_message(
                    "🎛 Control\n\n"
                    "The previous topic-rename attempt may have reached "
                    "Telegram, but its result was not recorded. I did not "
                    "repeat it. Check the topic name before retrying or "
                    "changing anything."
                )
            return

        if (
            mutation.state == "prepared"
            and binding.display_name != old_name
        ):
            raise StoreError(
                "Managed topic changed before the rename was confirmed."
            )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Renaming topic…",
                },
                "callback-answer",
            )
        if mutation.state == "prepared":
            with DurableStore(Path(database_path)) as store:
                mutation, acquired = store.begin_telegram_mutation_external(
                    action.operation_id
                )
            if mutation.state != "external_in_flight" or not acquired:
                if mutation.state == "external_in_flight" and not acquired:
                    send_message(
                        "🎛 Control\n\n"
                        "This confirmed topic rename is already being "
                        "processed. I did not send a second Telegram request."
                    )
                    return
                raise StoreError(
                    "Topic rename is not ready for its Telegram call."
                )
            try:
                result = bridge.api_call(
                    bridge.read_token(),
                    "editForumTopic",
                    chat_id=target_chat_id,
                    message_thread_id=target_thread_id,
                    name=new_name,
                )
            except bridge.BridgeError as exc:
                with DurableStore(Path(database_path)) as store:
                    store.require_telegram_mutation_reconciliation(
                        action.operation_id,
                        str(exc),
                    )
                send_message(
                    "🎛 Control\n\n"
                    "Telegram did not return a durable result for the topic "
                    "rename. I did not repeat the request automatically. "
                    "Check the topic name before trying again."
                )
                return
            if result is not True:
                with DurableStore(Path(database_path)) as store:
                    store.require_telegram_mutation_reconciliation(
                        action.operation_id,
                        "Telegram returned an invalid topic-rename result.",
                    )
                send_message(
                    "🎛 Control\n\n"
                    "Telegram returned an unexpected result for the topic "
                    "rename. I did not repeat the request."
                )
                return
            with DurableStore(Path(database_path)) as store:
                mutation = store.record_telegram_mutation_result(
                    action.operation_id,
                    {"telegram_result": True},
                )
        if mutation.state != "external_succeeded":
            raise StoreError("Topic rename has no durable Telegram result.")
        with DurableStore(Path(database_path)) as store:
            current = store.resolve_surface_binding_by_id(binding_id)
            if current is None or (
                current.chat_id,
                current.message_thread_id,
            ) != (target_chat_id, target_thread_id):
                raise StoreError(
                    "Managed topic changed before its rename could be recorded."
                )
            if current.display_name == old_name:
                store.rename_surface_binding(
                    binding_id=binding_id,
                    expected_chat_id=target_chat_id,
                    expected_message_thread_id=target_thread_id,
                    expected_display_name=old_name,
                    new_display_name=new_name,
                )
            elif current.display_name != new_name:
                raise StoreError(
                    "Managed topic changed before its rename could be recorded."
                )
            store.complete_telegram_mutation(action.operation_id)
        send_message(f"🎛 Control\n\nRenamed “{old_name}” to “{new_name}”.")
        return
    if action.action_type in {
        "router_config_confirm",
        "router_config_cancel",
    }:
        router_mailbox_id = int(action.payload.get("router_mailbox_id", 0))
        with DurableStore(Path(database_path)) as store:
            store.expire_router_config_actions(router_mailbox_id)
        if action.action_type == "router_config_cancel":
            if callback_query_id:
                deliver_api_call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_query_id,
                        "text": "Cancelled.",
                    },
                    "callback-answer",
                )
            send_message("🎛 Control\n\nCancelled. The configuration was not changed.")
            return
        project_slug = str(action.payload.get("project_slug", ""))
        updates = action.payload.get("updates")
        if not project_slug or not isinstance(updates, dict) or not updates:
            raise StoreError("Stored configuration plan is invalid.")
        try:
            with DurableStore(Path(database_path)) as store:
                project = store.resolve_project(project_slug)
                if project is None:
                    raise StoreError("The project is no longer enrolled.")
                target = store.resolve_project_agent(project.slug)
                if target is None:
                    raise StoreError(
                        "The project no longer has a managed agent."
                    )
                configured = store.configure_agent_provider(
                    target.agent_id,
                    {
                        key: (str(value) if value is not None else None)
                        for key, value in updates.items()
                        if key in {"model", "effort"}
                    },
                )
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
        configured_model_name, configured_effort_name = (
            provider_defaults.describe_provider_config(
                configured.provider,
                configured.provider_config,
                configured.project_path,
            )
        )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Configuration applied.",
                },
                "callback-answer",
            )
        send_message(
            "🎛 Control\n\n"
            f"Updated {project.display_name}.\n"
            f"Provider: {configured.provider}\n"
            f"Model: {configured_model_name}\n"
            f"Effort: {configured_effort_name}"
        )
        return
    if action.action_type in {
        "router_forum_workspace_confirm",
        "router_forum_workspace_cancel",
    }:
        router_mailbox_id = int(action.payload.get("router_mailbox_id", 0))
        with DurableStore(Path(database_path)) as store:
            store.expire_router_forum_workspace_actions(router_mailbox_id)
        if action.action_type == "router_forum_workspace_cancel":
            if callback_query_id:
                deliver_api_call(
                    "answerCallbackQuery",
                    {
                        "callback_query_id": callback_query_id,
                        "text": "Cancelled.",
                    },
                    "callback-answer",
                )
            send_message(
                "🎛 Control\n\n"
                "Cancelled. This forum was not bound to a workspace."
            )
            return

        required = {
            "chat_id",
            "forum_binding_id",
            "display_name",
            "project_path",
            "working_directory",
            "git_repository_root",
            "provider",
            "provider_config",
            "provenance",
        }
        if not required.issubset(action.payload):
            raise StoreError("Stored forum-workspace plan is invalid.")
        target_chat_id = int(action.payload["chat_id"])
        if target_chat_id != chat_id or target_chat_id >= 0:
            raise StoreError("Stored forum-workspace target is invalid.")
        provider_config = action.payload["provider_config"]
        if not isinstance(provider_config, dict):
            raise StoreError("Stored forum provider configuration is invalid.")
        workspace_root, working_directory, git_repository_root = (
            discovery.validate_agent_workspace(
                str(action.payload["project_path"]),
                str(action.payload["working_directory"]),
                (
                    str(action.payload["git_repository_root"])
                    if action.payload["git_repository_root"] is not None
                    else None
                ),
            )
        )
        if workspace_root != str(action.payload["project_path"]) or (
            working_directory != str(action.payload["working_directory"])
        ) or git_repository_root != action.payload["git_repository_root"]:
            raise StoreError(
                "The confirmed forum workspace no longer resolves to the "
                "validated locations."
            )
        with DurableStore(Path(database_path)) as store:
            workspace, created = store.bind_forum_workspace(
                chat_id=target_chat_id,
                forum_binding_id=int(action.payload["forum_binding_id"]),
                project_path=workspace_root,
                working_directory=working_directory,
                git_repository_root=git_repository_root,
                provider=str(action.payload["provider"]),
                provider_config=provider_config,
            )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": (
                        "Forum workspace bound."
                        if created
                        else "Forum workspace already bound."
                    ),
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
            "forum-workspace-clear",
        )
        send_message(
            "🎛 Control\n\n"
            f"✅ {workspace.display_name} is now bound to "
            f"{workspace.workspace_root}.\n\n"
            "New topics can now become workspace-scoped subjects."
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
            send_message("🎛 Control\n\nCancelled. No project or agent was created.")
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
            "working_directory",
            "git_repository_root",
            "topic_name",
            "provider_config",
            "provenance",
        }
        if not required.issubset(action.payload):
            raise StoreError("Stored project-creation plan is invalid.")
        # TOCTOU re-check: the confirmed workspace, optional Git metadata,
        # and symlink-resolved containment are validated exactly as at
        # proposal time.
        workspace_root, working_directory, git_repository_root = (
            discovery.validate_agent_workspace(
                str(action.payload["project_path"]),
                str(action.payload["working_directory"]),
                (
                    str(action.payload["git_repository_root"])
                    if action.payload["git_repository_root"] is not None
                    else None
                ),
            )
        )
        if workspace_root != str(action.payload["project_path"]) or (
            working_directory != str(action.payload["working_directory"])
        ) or git_repository_root != action.payload["git_repository_root"]:
            raise StoreError(
                "The confirmed workspace no longer resolves to the "
                "validated locations."
            )
        slug = str(action.payload["slug"])
        display_name = str(action.payload["display_name"])
        provider = str(action.payload["provider"])
        provider_config = action.payload["provider_config"]
        if not isinstance(provider_config, dict):
            raise StoreError("Stored provider configuration is invalid.")
        topic_name = str(action.payload["topic_name"])
        with DurableStore(Path(database_path)) as store:
            mutation = store.prepare_telegram_mutation(
                action.operation_id,
                "project_create",
                action.payload,
            )
        if mutation.state == "applied":
            with DurableStore(Path(database_path)) as store:
                agent = store.resolve_project_agent(slug)
            if agent is None:
                raise StoreError(
                    "Completed project creation references no managed agent."
                )
            send_message(
                "🎛 Control\n\n"
                f"{agent.hierarchical_name} is already attached to the "
                f"{topic_name} project topic."
            )
            return

        # Local enrollment is idempotent and occurs only after the confirmed
        # saga exists. If the process stops here, the same operation resumes
        # from its durable prepared stage.
        with DurableStore(Path(database_path)) as store:
            store.enroll_project(
                slug=slug,
                display_name=display_name,
                provider=provider,
                project_path=workspace_root,
                working_directory=working_directory,
                git_repository_root=git_repository_root,
            )
            existing_agent = store.resolve_project_agent(slug)
            existing_surface = store.resolve_named_surface(
                chat_id,
                topic_name,
                surface_type="project",
            )
        if existing_agent is not None:
            with DurableStore(Path(database_path)) as store:
                binding = (
                    store.resolve_surface_binding_by_id(
                        existing_agent.surface_binding_id
                    )
                    if existing_agent.surface_binding_id is not None
                    else None
                )
                if binding is None or binding.message_thread_id is None:
                    raise StoreError(
                        "Existing project agent has no Telegram topic."
                    )
                if mutation.state == "external_succeeded":
                    try:
                        stored_thread_id = int(
                            (mutation.external_result or {})[
                                "message_thread_id"
                            ]
                        )
                    except (KeyError, TypeError, ValueError):
                        raise StoreError(
                            "Stored project-topic result is invalid."
                        ) from None
                    if stored_thread_id != binding.message_thread_id:
                        raise StoreError(
                            "Stored project-topic result does not match the "
                            "attached agent."
                        )
                else:
                    store.record_telegram_mutation_result(
                        action.operation_id,
                        {
                            "message_thread_id": binding.message_thread_id,
                            "reconciled": "existing_agent",
                        },
                        reconciled=True,
                    )
                store.complete_telegram_mutation(action.operation_id)
            send_message(
                "🎛 Control\n\n"
                f"{display_name} already has managed agent "
                f"{existing_agent.hierarchical_name}."
            )
            return

        if mutation.state in {
            "external_in_flight",
            "reconciliation_required",
        }:
            if existing_surface is None:
                with DurableStore(Path(database_path)) as store:
                    store.require_telegram_mutation_reconciliation(
                        action.operation_id,
                        "The prior Telegram topic-creation call has no "
                        "durable result.",
                    )
                send_message(
                    "🎛 Control\n\n"
                    "The previous topic-creation request may have reached "
                    "Telegram, but its topic ID was not recorded. I did not "
                    "create a second topic. The project is enrolled, but its "
                    "Telegram topic and agent still need reconciliation."
                )
                return
            if existing_surface.message_thread_id is None:
                raise StoreError(
                    "Existing project surface is not a Telegram topic."
                )
            with DurableStore(Path(database_path)) as store:
                mutation = store.record_telegram_mutation_result(
                    action.operation_id,
                    {
                        "message_thread_id": existing_surface.message_thread_id,
                        "reconciled": "existing_surface",
                    },
                    reconciled=True,
                )

        if mutation.state == "prepared" and existing_surface is not None:
            if existing_surface.message_thread_id is None:
                raise StoreError(
                    "Existing project surface is not a Telegram topic."
                )
            with DurableStore(Path(database_path)) as store:
                mutation = store.record_telegram_mutation_result(
                    action.operation_id,
                    {
                        "message_thread_id": existing_surface.message_thread_id,
                        "reconciled": "existing_surface",
                    },
                    reconciled=True,
                )

        if mutation.state == "prepared" and existing_surface is None:
            with DurableStore(Path(database_path)) as store:
                mutation, acquired = store.begin_telegram_mutation_external(
                    action.operation_id
                )
            if mutation.state != "external_in_flight" or not acquired:
                if mutation.state == "external_in_flight" and not acquired:
                    send_message(
                        "🎛 Control\n\n"
                        "This confirmed project creation is already being "
                        "processed. I did not send a second Telegram request."
                    )
                    return
                raise StoreError(
                    "Project creation is not ready for its Telegram call."
                )
            try:
                topic = bridge.api_call(
                    bridge.read_token(),
                    "createForumTopic",
                    chat_id=chat_id,
                    name=topic_name,
                )
            except bridge.BridgeError as exc:
                with DurableStore(Path(database_path)) as store:
                    store.require_telegram_mutation_reconciliation(
                        action.operation_id,
                        str(exc),
                    )
                send_message(
                    "🎛 Control\n\n"
                    "Telegram did not return a durable topic ID. I did not "
                    "repeat the create request automatically. The project is "
                    "enrolled, but its Telegram topic and agent still need "
                    "reconciliation."
                )
                return
            try:
                project_thread_id = int(topic["message_thread_id"])
            except (KeyError, TypeError, ValueError):
                with DurableStore(Path(database_path)) as store:
                    store.require_telegram_mutation_reconciliation(
                        action.operation_id,
                        "Telegram returned an invalid project-topic result.",
                    )
                send_message(
                    "🎛 Control\n\n"
                    "Telegram returned an unexpected topic result. I did not "
                    "repeat the create request."
                )
                return
            with DurableStore(Path(database_path)) as store:
                mutation = store.record_telegram_mutation_result(
                    action.operation_id,
                    {"message_thread_id": project_thread_id},
                )
        if mutation.state != "external_succeeded":
            raise StoreError("Project creation has no durable Telegram result.")
        try:
            project_thread_id = int(
                (mutation.external_result or {})["message_thread_id"]
            )
        except (KeyError, TypeError, ValueError):
            raise StoreError(
                "Stored project-topic result is invalid."
            ) from None
        if project_thread_id <= 0:
            raise StoreError("Stored project-topic result is invalid.")
        with DurableStore(Path(database_path)) as store:
            store.ensure_surface_binding(
                chat_id=chat_id,
                message_thread_id=project_thread_id,
                surface_type="project",
                display_name=topic_name,
                target_type="controller",
                target_id="control",
            )
            agent, _ = store.attach_enrolled_project(
                chat_id,
                project_thread_id,
                slug,
                provider_config=provider_config,
            )
            store.complete_telegram_mutation(action.operation_id)
        send_message(
            "🎛 Control\n\n"
            f"Created {agent.hierarchical_name} in the "
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
                store.enqueue_topic_intro_refresh(agent_id)
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
    if action.action_type == "agent_configure_picker":
        agent_id = str(action.payload.get("agent_id", ""))
        provider = str(action.payload.get("provider", ""))
        if provider not in {"codex", "claude"}:
            raise StoreError("Agent configuration provider is invalid.")
        try:
            options = provider_adapters.configuration_options(provider)
            with DurableStore(Path(database_path)) as store:
                bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
                if bound_agent is None or bound_agent.agent_id != agent_id:
                    raise StoreError("Managed agent surface changed.")
                if bound_agent.provider != provider:
                    raise StoreError("Managed agent provider changed.")
                selected_model = bound_agent.provider_config.get("model")
                model_actions = []
                for label, model in options.models:
                    select = store.create_callback_action(
                        operation_id=(
                            f"callback:{update['update_id']}:"
                            f"agent-configure-model:{model or 'default'}"
                        ),
                        action_type="agent_configure_model_select",
                        payload={
                            "agent_id": agent_id,
                            "provider": provider,
                            "model": model,
                        },
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        authorized_user_id=user_id,
                        one_time=True,
                        ttl_seconds=15 * 60,
                    )
                    display_label = (
                        f"✓ {label}" if model == selected_model else label
                    )
                    model_actions.append((display_label, select))
                current_model, current_effort = (
                    provider_defaults.describe_provider_config(
                        provider,
                        bound_agent.provider_config,
                        bound_agent.project_path,
                    )
                )
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
        provider_name = "Claude" if provider == "claude" else "Codex"
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Choose a model.",
                },
                "callback-answer",
            )
        send_message(
            f"Change {provider_name} model\n\n"
            f"Current model: {current_model}\n"
            f"Current effort: {current_effort}\n\n"
            "The current conversation will be preserved.",
            reply_markup={
                "inline_keyboard": option_button_rows(model_actions),
            },
        )
        return
    if action.action_type == "agent_configure_model_select":
        agent_id = str(action.payload.get("agent_id", ""))
        provider = str(action.payload.get("provider", ""))
        model = action.payload.get("model")
        if provider not in {"codex", "claude"}:
            raise StoreError("Agent configuration provider is invalid.")
        options = provider_adapters.configuration_options(provider)
        model_labels = {value: label for label, value in options.models}
        if model not in model_labels:
            raise StoreError("Stored agent model selection is invalid.")
        try:
            with DurableStore(Path(database_path)) as store:
                bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
                if bound_agent is None or bound_agent.agent_id != agent_id:
                    raise StoreError("Managed agent surface changed.")
                if bound_agent.provider != provider:
                    raise StoreError("Managed agent provider changed.")
                selected_effort = bound_agent.provider_config.get("effort")
                effort_actions = []
                for label, effort in options.efforts:
                    select = store.create_callback_action(
                        operation_id=(
                            f"callback:{update['update_id']}:"
                            f"agent-configure-effort:{effort or 'default'}"
                        ),
                        action_type="agent_configure_effort_select",
                        payload={
                            "agent_id": agent_id,
                            "provider": provider,
                            "model": model,
                            "effort": effort,
                        },
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        authorized_user_id=user_id,
                        one_time=True,
                        ttl_seconds=15 * 60,
                    )
                    display_label = (
                        f"✓ {label}" if effort == selected_effort else label
                    )
                    effort_actions.append((display_label, select))
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
        model_config = {"model": str(model)} if model is not None else {}
        model_name, _ = provider_defaults.describe_provider_config(
            provider,
            model_config,
            bound_agent.project_path,
        )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Choose an effort level.",
                },
                "callback-answer",
            )
        send_message(
            f"Model: {model_name}\n\nChoose effort.\n\n"
            "The current conversation will be preserved.",
            reply_markup={
                "inline_keyboard": option_button_rows(effort_actions),
            },
        )
        return
    if action.action_type == "agent_configure_effort_select":
        agent_id = str(action.payload.get("agent_id", ""))
        provider = str(action.payload.get("provider", ""))
        model = action.payload.get("model")
        effort = action.payload.get("effort")
        if provider not in {"codex", "claude"}:
            raise StoreError("Agent configuration provider is invalid.")
        options = provider_adapters.configuration_options(provider)
        if model not in {value for _, value in options.models}:
            raise StoreError("Stored agent model selection is invalid.")
        if effort not in {value for _, value in options.efforts}:
            raise StoreError("Stored agent effort selection is invalid.")
        try:
            with DurableStore(Path(database_path)) as store:
                bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
                if bound_agent is None or bound_agent.agent_id != agent_id:
                    raise StoreError("Managed agent surface changed.")
                if bound_agent.provider != provider:
                    raise StoreError("Managed agent provider changed.")
                previous_session_id = bound_agent.provider_session_id
                configured = store.configure_agent_provider(
                    agent_id,
                    {
                        "model": str(model) if model is not None else None,
                        "effort": str(effort) if effort is not None else None,
                    },
                )
                session_preserved = (
                    configured.provider_session_id == previous_session_id
                )
                # The topic's header reports model and effort, so it must not
                # keep advertising the settings this call just replaced.
                store.enqueue_topic_intro_refresh(agent_id)
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
        model_name, effort_name = provider_defaults.describe_provider_config(
            configured.provider,
            configured.provider_config,
            configured.project_path,
        )
        provider_name = "Claude" if provider == "claude" else "Codex"
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": "Model and effort updated.",
                },
                "callback-answer",
            )
        session_note = (
            "The current conversation is preserved."
            if configured.provider_session_id and session_preserved
            else "The next message will use these settings."
        )
        send_message(
            f"✅ Updated {provider_name} settings.\n"
            f"Model: {model_name}\n"
            f"Effort: {effort_name}\n\n"
            f"{session_note}"
        )
        return
    if action.action_type == "agent_switch_provider_prompt":
        agent_id = str(action.payload.get("agent_id", ""))
        provider = str(action.payload.get("provider", ""))
        expected_provider = str(action.payload.get("expected_provider", ""))
        if (
            provider not in {"codex", "claude"}
            or expected_provider not in {"codex", "claude"}
            or provider == expected_provider
        ):
            raise StoreError("Provider switch selection is invalid.")
        try:
            with DurableStore(Path(database_path)) as store:
                bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
                if bound_agent is None or bound_agent.agent_id != agent_id:
                    raise StoreError("Managed agent surface changed.")
                if bound_agent.provider != expected_provider:
                    raise StoreError("Managed agent provider changed.")
                confirm = store.create_callback_action(
                    operation_id=(
                        f"callback:{update['update_id']}:"
                        "agent-switch-provider-confirm"
                    ),
                    action_type="agent_switch_provider_confirm",
                    payload={
                        "agent_id": agent_id,
                        "expected_provider": expected_provider,
                        "provider": provider,
                    },
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    authorized_user_id=user_id,
                    one_time=True,
                    ttl_seconds=5 * 60,
                )
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
        provider_name = "Claude" if provider == "claude" else "Codex"
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
            f"Switch this topic to {provider_name}?\n\n"
            f"The existing {expected_provider.title()} conversation remains "
            "persisted locally. The next message will start a fresh "
            f"{provider_name} conversation; you can also choose one of its "
            "existing sessions afterward.",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": f"Confirm switch to {provider_name}",
                            "callback_data": f"a:{confirm.token}",
                        }
                    ]
                ]
            },
        )
        return
    if action.action_type == "agent_switch_provider_confirm":
        agent_id = str(action.payload.get("agent_id", ""))
        provider = str(action.payload.get("provider", ""))
        expected_provider = str(action.payload.get("expected_provider", ""))
        try:
            with DurableStore(Path(database_path)) as store:
                bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
                if bound_agent is None or bound_agent.agent_id != agent_id:
                    raise StoreError("Managed agent surface changed.")
                switched = store.switch_agent_provider(
                    agent_id,
                    provider,
                    expected_provider,
                )
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
        provider_name = "Claude" if switched.provider == "claude" else "Codex"
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": f"Switched to {provider_name}.",
                },
                "callback-answer",
            )
        send_message(
            f"✅ This topic now uses {provider_name}. Its next message will "
            "start a fresh conversation."
        )
        return
    if action.action_type == "agent_new_session_prompt":
        agent_id = str(action.payload.get("agent_id", ""))
        with DurableStore(Path(database_path)) as store:
            bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
            if bound_agent is None or bound_agent.agent_id != agent_id:
                raise StoreError("Managed agent surface changed.")
            provider_name = (
                "Claude" if bound_agent.provider == "claude" else "Codex"
            )
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
            f"Start a fresh {provider_name} conversation?\n\n"
            f"The current conversation is retained by {provider_name}, but new messages "
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
                provider_name = (
                    "Claude" if bound_agent.provider == "claude" else "Codex"
                )
                store.reset_agent_session(agent_id)
                # A fresh session has no context yet; the header should say so
                # rather than keep the retired session's last measurement.
                store.enqueue_topic_intro_refresh(agent_id)
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
        send_message(
            f"✅ The next message will start a fresh {provider_name} conversation."
        )
        return
    if action.action_type == "agent_resume_session_picker":
        agent_id = str(action.payload.get("agent_id", ""))
        provider = str(action.payload.get("provider", ""))
        with DurableStore(Path(database_path)) as store:
            bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
            if bound_agent is None or bound_agent.agent_id != agent_id:
                raise StoreError("Managed agent surface changed.")
            if bound_agent.provider != provider or provider not in {"codex", "claude"}:
                raise StoreError("Managed agent provider changed.")
            provider_name = "Claude" if provider == "claude" else "Codex"
            session_source = (
                claude_sessions if provider == "claude" else codex_sessions
            )
            working_directory = (
                bound_agent.working_directory or bound_agent.project_path
            )
            if not working_directory:
                raise StoreError("Managed agent has no working directory.")
            used_session_ids = store.registered_provider_session_ids(
                provider=provider,
                excluding_agent_id=agent_id
            )
            if bound_agent.provider_session_id:
                used_session_ids.add(bound_agent.provider_session_id)
            snapshot_operation_id = (
                f"callback:{update['update_id']}:"
                "agent-resume-session-snapshot"
            )
            snapshot = store.resolve_callback_action_operation(
                snapshot_operation_id
            )
            if snapshot is None:
                discovered = session_source.discover_sessions(
                    working_directory,
                    excluded_session_ids=used_session_ids,
                )
                snapshot = store.create_callback_action(
                    operation_id=snapshot_operation_id,
                    action_type="agent_resume_session_snapshot",
                    payload={
                        "agent_id": agent_id,
                        "provider": provider,
                        "candidates": [
                            {
                                "provider_session_id": candidate.session_id,
                                "label": candidate.button_label(),
                            }
                            for candidate in discovered
                        ],
                    },
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    authorized_user_id=user_id,
                    one_time=False,
                    ttl_seconds=5 * 60,
                )
            store.retire_callback_action_operation(
                snapshot_operation_id,
                "agent_resume_session_snapshot",
            )
            if (
                snapshot.payload.get("agent_id") != agent_id
                or snapshot.payload.get("provider") != provider
            ):
                raise StoreError("Persisted session picker snapshot is invalid.")
            candidates = snapshot.payload.get("candidates")
            if not isinstance(candidates, list) or len(candidates) > 5:
                raise StoreError("Persisted session picker snapshot is invalid.")
            buttons = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    raise StoreError(
                        "Persisted session picker snapshot is invalid."
                    )
                candidate_session_id = str(
                    candidate.get("provider_session_id", "")
                )
                candidate_label = str(candidate.get("label", ""))
                if (
                    session_source.SESSION_ID_PATTERN.fullmatch(
                        candidate_session_id
                    )
                    is None
                    or not candidate_label
                    or len(candidate_label) > 64
                ):
                    raise StoreError(
                        "Persisted session picker snapshot is invalid."
                    )
                select = store.create_callback_action(
                    operation_id=(
                        f"callback:{update['update_id']}:"
                        "agent-resume-session-prompt:"
                        f"{candidate_session_id}"
                    ),
                    action_type="agent_resume_session_prompt",
                    payload={
                        "agent_id": agent_id,
                        "provider": provider,
                        "provider_session_id": candidate_session_id,
                        "candidate_label": candidate_label,
                    },
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    authorized_user_id=user_id,
                    one_time=True,
                    ttl_seconds=5 * 60,
                )
                buttons.append(
                    [
                        {
                            "text": candidate_label,
                            "callback_data": f"a:{select.token}",
                        }
                    ]
                )
        if callback_query_id:
            deliver_api_call(
                "answerCallbackQuery",
                {
                    "callback_query_id": callback_query_id,
                    "text": (
                        "Choose a persisted session."
                        if candidates
                        else "No compatible sessions found."
                    ),
                },
                "callback-answer",
            )
        if not candidates:
            send_message(
                f"No other persisted {provider_name} sessions were found for this "
                "topic’s working directory."
            )
            return
        send_message(
            f"Resume a persisted {provider_name} session\n\n"
            "Choose a recent session from this exact working directory. "
            "This can resume a closed or dormant session; it cannot safely "
            f"take control of a turn that is still running in another "
            f"{provider_name} window.",
            reply_markup={"inline_keyboard": buttons},
        )
        return
    if action.action_type == "agent_resume_session_prompt":
        agent_id = str(action.payload.get("agent_id", ""))
        provider = str(action.payload.get("provider", ""))
        provider_session_id = str(
            action.payload.get("provider_session_id", "")
        )
        candidate_label = str(action.payload.get("candidate_label", ""))
        if not candidate_label or len(candidate_label) > 64:
            raise StoreError("Persisted session selection is invalid.")
        try:
            with DurableStore(Path(database_path)) as store:
                bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
                if bound_agent is None or bound_agent.agent_id != agent_id:
                    raise StoreError("Managed agent surface changed.")
                if (
                    bound_agent.provider != provider
                    or provider not in {"codex", "claude"}
                ):
                    raise StoreError("Managed agent provider changed.")
                provider_name = "Claude" if provider == "claude" else "Codex"
                session_source = (
                    claude_sessions if provider == "claude" else codex_sessions
                )
                confirm_operation_id = (
                    f"callback:{update['update_id']}:"
                    "agent-resume-session-confirm"
                )
                confirm = store.resolve_callback_action_operation(
                    confirm_operation_id
                )
                if confirm is None:
                    working_directory = (
                        bound_agent.working_directory
                        or bound_agent.project_path
                    )
                    candidate = (
                        session_source.resolve_session(
                            provider_session_id,
                            working_directory,
                        )
                        if working_directory
                        else None
                    )
                    if candidate is None:
                        raise StoreError(
                            "That persisted session is no longer available for "
                            "this working directory."
                        )
                    confirm = store.create_callback_action(
                        operation_id=confirm_operation_id,
                        action_type="agent_resume_session_confirm",
                        payload={
                            "agent_id": agent_id,
                            "provider": provider,
                            "provider_session_id": candidate.session_id,
                            "candidate_label": candidate_label,
                            "expected_provider_session_id": (
                                bound_agent.provider_session_id
                            ),
                        },
                        chat_id=chat_id,
                        message_thread_id=thread_id,
                        authorized_user_id=user_id,
                        one_time=True,
                        ttl_seconds=5 * 60,
                    )
                if (
                    confirm.payload.get("agent_id") != agent_id
                    or confirm.payload.get("provider") != provider
                    or confirm.payload.get("provider_session_id")
                    != provider_session_id
                    or confirm.payload.get("candidate_label")
                    != candidate_label
                ):
                    raise StoreError(
                        "Persisted session confirmation is invalid."
                    )
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
                    "text": "Confirmation required.",
                },
                "callback-answer",
            )
        send_message(
            f"Resume this {provider_name} session?\n\n"
            f"{candidate_label}\n\n"
            "The topic’s current conversation remains stored, but future "
            "messages will continue the selected session. Close any active "
            "turn using it elsewhere first.",
            reply_markup={
                "inline_keyboard": [
                    [
                        {
                            "text": "Confirm resume",
                            "callback_data": f"a:{confirm.token}",
                        }
                    ]
                ]
            },
        )
        return
    if action.action_type == "agent_resume_session_confirm":
        agent_id = str(action.payload.get("agent_id", ""))
        provider = str(action.payload.get("provider", ""))
        provider_session_id = str(
            action.payload.get("provider_session_id", "")
        )
        expected_provider_session_id = action.payload.get(
            "expected_provider_session_id"
        )
        if expected_provider_session_id is not None:
            expected_provider_session_id = str(expected_provider_session_id)
        try:
            with DurableStore(Path(database_path)) as store:
                bound_agent = store.resolve_agent_for_surface(chat_id, thread_id)
                if bound_agent is None or bound_agent.agent_id != agent_id:
                    raise StoreError("Managed agent surface changed.")
                if (
                    bound_agent.provider != provider
                    or provider not in {"codex", "claude"}
                ):
                    raise StoreError("Managed agent provider changed.")
                provider_name = "Claude" if provider == "claude" else "Codex"
                session_source = (
                    claude_sessions if provider == "claude" else codex_sessions
                )
                working_directory = (
                    bound_agent.working_directory or bound_agent.project_path
                )
                candidate = (
                    session_source.resolve_session(
                        provider_session_id,
                        working_directory,
                    )
                    if working_directory
                    else None
                )
                if candidate is None:
                    raise StoreError(
                        "That persisted session is no longer available for this "
                        "working directory."
                    )
                store.adopt_agent_session(
                    agent_id,
                    candidate.session_id,
                    expected_provider_session_id,
                )
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
                    "text": f"{provider_name} session resumed.",
                },
                "callback-answer",
            )
        send_message(
            f"✅ This topic will continue the selected {provider_name} session on its "
            "next message."
        )
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


def inbound_attachment(message: dict) -> Optional[dict]:
    """Return the best Telegram photo or document descriptor, if present."""
    photos = message.get("photo")
    if isinstance(photos, list):
        candidates = [item for item in photos if isinstance(item, dict)]
        if candidates:
            selected = max(
                candidates,
                key=lambda item: (
                    int(item.get("file_size", 0)),
                    int(item.get("width", 0)) * int(item.get("height", 0)),
                ),
            )
            return {**selected, "safe_filename": "photo.jpg", "kind": "image"}
    document = message.get("document")
    if not isinstance(document, dict):
        return None
    original_name = Path(str(document.get("file_name") or "document")).name
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", original_name).strip("._")
    if not safe_name:
        safe_name = "document"
    return {**document, "safe_filename": safe_name[:180], "kind": "document"}


def persist_inbound_attachment(attachment: dict) -> Path:
    """Download an attachment once to a private path stable across retries."""
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    if not database_path or not job_id:
        raise StoreError("Attachment input requires the durable controller.")
    file_id = str(attachment.get("file_id", ""))
    if not file_id:
        raise bridge.BridgeError("Telegram attachment has no downloadable file ID.")
    file_size = int(attachment.get("file_size", 0))
    if file_size > MAX_ATTACHMENT_BYTES:
        raise bridge.BridgeError("Attachment exceeds the configured 20 MB limit.")
    unique_id = re.sub(
        r"[^A-Za-z0-9_-]",
        "_",
        str(attachment.get("file_unique_id") or file_id),
    )[:128]
    safe_filename = str(attachment["safe_filename"])
    attachment_dir = (
        Path(database_path).expanduser().resolve().parent
        / "attachments"
        / f"inbox-{int(job_id)}"
    )
    attachment_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    attachment_dir.chmod(0o700)
    destination = attachment_dir / f"{unique_id}--{safe_filename}"
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    partial = attachment_dir / f".{unique_id}--{safe_filename}.part"
    partial.unlink(missing_ok=True)
    try:
        bridge.download_telegram_file(
            file_id,
            partial,
            max_bytes=MAX_ATTACHMENT_BYTES,
        )
        if not partial.is_file() or partial.stat().st_size <= 0:
            raise bridge.BridgeError("Telegram attachment download was empty.")
        bridge.ensure_private_file(partial)
        os.replace(partial, destination)
        bridge.ensure_private_file(destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def attachment_prompt(path: Path, caption: str, kind: str) -> str:
    prompt = (
        f"The user sent a Telegram {kind}. Inspect the local file at this absolute "
        f"path and use it as part of their request:\n{path}"
    )
    caption = caption.strip()
    if caption:
        prompt += f"\n\nUser caption:\n{caption}"
    else:
        prompt += (
            "\n\nThe attachment had no caption. Respond based on its contents."
        )
    return prompt


def transcribing_receipt(
    store: DurableStore,
    agent_id: str,
    chat_id: int,
    thread_id: Optional[int],
) -> str:
    """Name the agent only when the surface does not already."""
    speaker = html.escape(
        store.agent_surface_header(agent_id, chat_id, thread_id)
    )
    if speaker:
        return f"🎙️ <b>{speaker} is transcribing…</b>"
    return "🎙️ <b>Transcribing…</b>"


def transcribe_voice_note(voice: dict) -> str:
    """Download one Telegram voice note and transcribe it locally."""
    with tempfile.TemporaryDirectory(
        prefix="telegram-voice-"
    ) as temporary_directory:
        temp_dir = Path(temporary_directory)
        source_path = temp_dir / "voice.ogg"
        wav_path = temp_dir / "voice.wav"
        bridge.download_telegram_file(
            str(voice["file_id"]),
            source_path,
            max_bytes=MAX_VOICE_BYTES,
        )
        convert_to_wav(source_path, wav_path)
        return transcribe_wav(wav_path)


def handle_voice(update: dict, voice: dict) -> None:
    file_size = int(voice.get("file_size", 0))
    duration = int(voice.get("duration", 0))
    if file_size > MAX_VOICE_BYTES:
        raise bridge.BridgeError("Voice message exceeds Telegram's 20 MB bot download limit.")
    if duration > MAX_VOICE_SECONDS:
        raise bridge.BridgeError("Voice message is longer than the configured 30-minute limit.")
    message = update.get("message") or {}
    sender = message.get("from") or {}
    try:
        authorized_user_id = int(sender["id"])
    except (KeyError, TypeError, ValueError):
        raise StoreError("Voice message sender identity is unavailable.") from None

    binding = current_surface_binding()
    managed_agent = None
    routes_to_main_router = False
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    reply_route = None
    if os.environ.get("TELEGRAM_REPLY_TO_MESSAGE_ID"):
        reply_route = resolve_replied_message_route()
    agent_reply_route = None
    router_reply_route = None
    if reply_route is not None and reply_route.target_type == "agent":
        if not (
            binding is not None
            and binding.target_type == "agent"
            and binding.target_id == reply_route.target_id
        ):
            # The voice note replies to an agent-routed message outside that
            # agent's own topic; continue that agent on the reply surface.
            agent_reply_route = reply_route
    elif (
        reply_route is not None
        and reply_route.target_type == "controller"
        and reply_route.target_id == "control"
    ):
        router_reply_route = reply_route
    if agent_reply_route is not None and database_path and job_id:
        chat_id, thread_id = surface_coordinates()
        with DurableStore(Path(database_path)) as store:
            managed_agent = store.resolve_agent(agent_reply_route.target_id)
            if managed_agent is None or managed_agent.role not in {
                "project",
                "worker",
            }:
                raise StoreError("Managed agent route is no longer valid.")
            store.enqueue_agent_reply_receipt(
                agent_id=managed_agent.agent_id,
                source_inbox_job_id=int(job_id),
                chat_id=chat_id,
                message_thread_id=thread_id,
                replied_message_id=agent_reply_route.telegram_message_id,
                receipt_text=transcribing_receipt(
                    store,
                    managed_agent.agent_id,
                    chat_id,
                    thread_id,
                ),
                input_kind="voice",
                parse_mode="HTML",
            )
    elif router_reply_route is not None and database_path and job_id:
        chat_id, thread_id = surface_coordinates()
        routes_to_main_router = True
        with DurableStore(Path(database_path)) as store:
            store.enqueue_router_voice_receipt(
                source_inbox_job_id=int(job_id),
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=authorized_user_id,
                replied_message_id=router_reply_route.telegram_message_id,
            )
    elif (
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
                receipt_text=transcribing_receipt(
                    store,
                    managed_agent.agent_id,
                    chat_id,
                    thread_id,
                ),
                input_kind="voice",
                parse_mode="HTML",
            )
    elif database_path and job_id:
        chat_id, thread_id = surface_coordinates()
        if binding is None:
            with DurableStore(Path(database_path)) as store:
                binding = store.ensure_surface_binding(
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    surface_type="control",
                    display_name=surface_display_name(),
                    target_type="controller",
                    target_id="control",
                )
        if (
            binding is not None
            and binding.target_type == "controller"
            and binding.target_id == "control"
        ):
            routes_to_main_router = True
            with DurableStore(Path(database_path)) as store:
                store.enqueue_router_voice_receipt(
                    source_inbox_job_id=int(job_id),
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    authorized_user_id=authorized_user_id,
                )
        else:
            send_message(
                "🎙️ Voice message received. Transcribing locally with "
                "Parakeet V3…"
            )
    else:
        send_message("🎙️ Voice message received. Transcribing locally with Parakeet V3…")

    transcript = transcribe_voice_note(voice)

    if managed_agent is not None:
        agent_input = transcript or (
            "The user's voice note contained no detectable speech. "
            "Briefly ask them to try recording it again."
        )
        chat_id, thread_id = surface_coordinates()
        with DurableStore(Path(database_path)) as store:
            if (
                reply_route is not None
                and reply_route.target_type == "agent"
                and reply_route.target_id == managed_agent.agent_id
            ):
                control = store.enqueue_agent_steer_from_receipt(
                    agent_id=managed_agent.agent_id,
                    source_inbox_job_id=int(job_id),
                    input_text=agent_input,
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    replied_message_id=reply_route.telegram_message_id,
                    input_kind="voice",
                )
                if control is not None:
                    return
            if agent_reply_route is not None:
                store.enqueue_agent_reply_voice_message(
                    agent_id=managed_agent.agent_id,
                    source_inbox_job_id=int(job_id),
                    input_text=agent_input,
                    chat_id=chat_id,
                    message_thread_id=thread_id,
                    replied_message_id=(
                        agent_reply_route.telegram_message_id
                    ),
                    authorized_user_id=authorized_user_id,
                )
            else:
                store.enqueue_agent_voice_message(
                    agent_id=managed_agent.agent_id,
                    source_inbox_job_id=int(job_id),
                    input_text=agent_input,
                    authorized_user_id=authorized_user_id,
                )
    elif routes_to_main_router:
        router_input = transcript or (
            "The user's voice note contained no detectable speech. "
            "Briefly ask them to try recording it again."
        )
        if router_reply_route is not None and transcript:
            router_input = build_router_reply_input(
                update,
                router_reply_route,
                transcript,
            )
        elif router_reply_route is None:
            router_input = router_input_for_surface(router_input)
        chat_id, thread_id = surface_coordinates()
        with DurableStore(Path(database_path)) as store:
            store.enqueue_router_voice_message(
                source_inbox_job_id=int(job_id),
                input_text=router_input,
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=authorized_user_id,
                replied_message_id=(
                    router_reply_route.telegram_message_id
                    if router_reply_route is not None
                    else None
                ),
            )
    elif transcript:
        send_message(f"📝 Transcript:\n\n{transcript}")
    else:
        send_message("📝 Parakeet did not detect any speech in that voice message.")


def awaiting_forum_workspace() -> bool:
    """Report an authorized private forum that has no workspace yet."""
    if os.environ.get("TELEGRAM_CHAT_TYPE") != "supergroup":
        return False
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    chat_id_text = os.environ.get("TELEGRAM_CHAT_ID")
    if not database_path or not chat_id_text:
        return False
    chat_id = int(chat_id_text)
    with DurableStore(Path(database_path)) as store:
        forum = store.resolve_surface_binding(chat_id, None)
        if (
            forum is None
            or forum.target_type != "controller"
            or forum.target_id != "control"
        ):
            return False
        return store.resolve_forum_workspace(chat_id) is None


def router_input_for_surface(text: str) -> str:
    """Tell Control when a message answers the pending workspace question."""
    if awaiting_forum_workspace():
        return compose_forum_setup_input(text)
    return text


def enqueue_attachment_receipt() -> Optional[str]:
    """Queue the eventual turn card before downloading a routed attachment."""
    database_path = os.environ.get("TELEGRAM_CONTROL_DB")
    job_id = os.environ.get("TELEGRAM_CONTROL_JOB_ID")
    user_id = os.environ.get("TELEGRAM_FROM_ID")
    if not database_path or not job_id or not user_id:
        raise StoreError("Attachment receipt requires the durable controller.")
    chat_id, thread_id = surface_coordinates()
    receipt_text = "📎 <b>Attachment received. Downloading securely…</b>"
    reply_route = (
        resolve_replied_message_route()
        if os.environ.get("TELEGRAM_REPLY_TO_MESSAGE_ID")
        else None
    )
    binding = current_surface_binding()
    with DurableStore(Path(database_path)) as store:
        if reply_route is not None and reply_route.target_type == "agent":
            store.enqueue_agent_reply_receipt(
                agent_id=reply_route.target_id,
                source_inbox_job_id=int(job_id),
                chat_id=chat_id,
                message_thread_id=thread_id,
                replied_message_id=reply_route.telegram_message_id,
                receipt_text=receipt_text,
                input_kind="text",
                parse_mode="HTML",
            )
            return receipt_text
        if (
            reply_route is not None
            and reply_route.target_type == "controller"
            and reply_route.target_id == "control"
        ):
            store.enqueue_router_receipt(
                source_inbox_job_id=int(job_id),
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=int(user_id),
                receipt_text=receipt_text,
                parse_mode="HTML",
                replied_message_id=reply_route.telegram_message_id,
            )
            return receipt_text
        if binding is not None and binding.target_type == "agent":
            store.enqueue_agent_receipt(
                agent_id=binding.target_id,
                source_inbox_job_id=int(job_id),
                chat_id=chat_id,
                message_thread_id=thread_id,
                receipt_text=receipt_text,
                input_kind="text",
                parse_mode="HTML",
            )
            return receipt_text
        if (
            binding is not None
            and binding.target_type == "controller"
            and binding.target_id == "control"
        ):
            store.enqueue_router_receipt(
                source_inbox_job_id=int(job_id),
                chat_id=chat_id,
                message_thread_id=thread_id,
                authorized_user_id=int(user_id),
                receipt_text=receipt_text,
                parse_mode="HTML",
            )
            return receipt_text
    return None


def route_user_input(
    update: dict,
    text: str,
    receipt_text: Optional[str] = None,
) -> None:
    """Route normalized text, including prompts that reference attachments."""
    replied_message_id = os.environ.get("TELEGRAM_REPLY_TO_MESSAGE_ID")
    if replied_message_id:
        route = resolve_replied_message_route()
        if (
            route is not None
            and route.target_type == "controller"
            and route.target_id == "control"
        ):
            enqueue_router_input(
                build_router_reply_input(update, route, text),
                replied_message_id=route.telegram_message_id,
                receipt_text=receipt_text,
            )
        elif route is not None and route.target_type == "agent":
            if not try_enqueue_agent_steer(route, text):
                binding = current_surface_binding()
                if (
                    binding is not None
                    and binding.target_type == "agent"
                    and binding.target_id == route.target_id
                ):
                    enqueue_agent_input(
                        route.target_id,
                        text,
                        receipt_text=receipt_text,
                    )
                else:
                    enqueue_agent_reply_input(
                        route,
                        text,
                        receipt_text=receipt_text,
                    )
        else:
            send_message("That replied-to message has no active durable route.")
        return

    if prompt_forum_subject_provider_selection(
        request_was_already_sent=True,
        pending_request=text,
    ):
        return
    ensure_bound_forum_subject()
    binding = current_surface_binding()
    thread_id = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID")
    router_text = router_input_for_surface(text)
    if thread_id and binding is None:
        enqueue_router_input(router_text, receipt_text=receipt_text)
    elif thread_id and binding is not None:
        if binding.target_type == "agent":
            enqueue_agent_input(
                binding.target_id,
                text,
                receipt_text=receipt_text,
            )
        elif (
            binding.target_type == "controller"
            and binding.target_id == "control"
        ):
            enqueue_router_input(router_text, receipt_text=receipt_text)
        else:
            send_message(
                "This topic's controller binding does not accept messages yet."
            )
    else:
        enqueue_router_input(router_text, receipt_text=receipt_text)


def main() -> int:
    update = json.load(sys.stdin)
    username = os.environ.get("TELEGRAM_FROM_USERNAME") or "unknown"

    try:
        callback_query = update.get("callback_query")
        message = update.get("message")
        if callback_query:
            handle_callback(update, callback_query)
        elif (
            message
            and not isinstance(message.get("forum_topic_created"), dict)
            and handle_report_only_topic()
        ):
            return 0
        elif message and isinstance(message.get("forum_topic_created"), dict):
            topic_name = str(message["forum_topic_created"].get("name", ""))
            print(f"Received new forum topic: {topic_name}", flush=True)
            if forum_is_authorized_or_prompt():
                prompt_forum_subject_provider_selection()
        elif message and "voice" in message:
            print(f"Received voice message from @{username}.", flush=True)
            if forum_is_authorized_or_prompt():
                if (
                    not os.environ.get("TELEGRAM_REPLY_TO_MESSAGE_ID")
                    and forum_subject_setup_pending()
                ):
                    # Transcribe before the setup card so the voice note itself
                    # can run as the first turn; re-recording it would be worse
                    # than the few seconds this costs.
                    if prompt_forum_subject_provider_selection(
                        request_was_already_sent=True,
                        pending_request=transcribe_voice_note(
                            message["voice"]
                        ),
                    ):
                        return 0
                handle_voice(update, message["voice"])
        elif message and inbound_attachment(message) is not None:
            print(f"Received attachment from @{username}.", flush=True)
            caption = str(message.get("caption", ""))
            if not forum_is_authorized_or_prompt(caption):
                return 0
            attachment = inbound_attachment(message)
            if attachment is None:
                raise bridge.BridgeError(
                    "Telegram attachment metadata is unavailable."
                )
            # Queue the eventual turn card before getFile or the content
            # download so a slow transfer never delays user feedback.
            receipt_text = enqueue_attachment_receipt()
            path = persist_inbound_attachment(attachment)
            route_user_input(
                update,
                attachment_prompt(path, caption, str(attachment["kind"])),
                receipt_text=receipt_text,
            )
        elif message and "text" in message:
            text = str(message["text"])
            print(f"Received text message from @{username}: {text}", flush=True)
            if not forum_is_authorized_or_prompt(text):
                return 0
            replied_message_id = os.environ.get("TELEGRAM_REPLY_TO_MESSAGE_ID")
            command = addressed_command(text)
            agent_create = re.fullmatch(
                r"/agent\s+create\s+([a-z0-9]+(?:-[a-z0-9]+)*)",
                command.lower(),
            )
            if command.lower() == "/status":
                binding = current_surface_binding()
                if binding is not None and binding.target_type == "agent":
                    send_agent_status()
                else:
                    send_status_card(update)
            elif command.lower() == "/help":
                send_help_menu()
            elif command.lower() == "/teardown":
                request_topic_teardown()
            elif command.lower() == "/projects":
                send_project_catalog()
            elif command.lower() == "/newgroup":
                send_group_setup_card()
            elif re.fullmatch(r"/start(?:\s.*)?", command, re.DOTALL):
                # The startgroup deep link delivers /start with a payload once
                # Telegram finishes adding the bot. That is an arrival, not a
                # request, so it only ever offers authorization.
                if os.environ.get("TELEGRAM_CHAT_TYPE") == "supergroup":
                    forum_is_authorized_or_prompt()
                else:
                    send_message(
                        "I am already paired with this chat.\n\n"
                        "Send /newgroup to add me to a project group, or "
                        f"just tell me what you need.\n\n{telegram_help.HELP_HINT}"
                    )
            elif agent_create is not None:
                create_agent_from_catalog(agent_create.group(1))
            elif command.lower() in {"/agent", "/agent status"}:
                if not prompt_forum_subject_provider_selection():
                    send_agent_status()
            else:
                route_user_input(update, text)
        else:
            send_message(
                "Send me text, an image, or a Telegram voice message.\n\n"
                f"{telegram_help.HELP_HINT}"
            )
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

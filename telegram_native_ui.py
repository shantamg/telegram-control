"""Native Telegram UI primitives that do not require a Mini App."""

from __future__ import annotations

from typing import Any, Callable, Optional

import telegram_bridge


SHOWCASE_RICH_HTML = """\
<h2>🎛 Control · Native UI showcase</h2>
<p>This is a real <b>Telegram rich message</b>, not a web view. It can combine structured content with the same inline keyboard used by ordinary cards.</p>
<hr/>
<table bordered striped>
<caption>Useful surfaces</caption>
<tr><th>Surface</th><th>Telegram Control use</th></tr>
<tr><td>Rich blocks</td><td>Reports, plans and technical answers</td></tr>
<tr><td>Force Reply</td><td>Folder paths and clarification prompts</td></tr>
<tr><td>Reactions</td><td>Low-clutter receipt and completion state</td></tr>
<tr><td>Ephemeral</td><td>Private hints inside a group</td></tr>
</table>
<details>
<summary>What remains deliberately constrained</summary>
<p>The composer menu can open commands without a Mini App. Attachment-menu and arbitrary custom composer interfaces still require a Mini App.</p>
</details>
<p>Try the colored buttons below. Each copies a harmless value so the showcase has no side effects.</p>"""

SHOWCASE_FALLBACK_HTML = """\
<b>🎛 Control · Native UI showcase</b>

This client or Bot API endpoint did not accept Telegram’s newer rich-message
format, so Control used ordinary native HTML instead.

• Rich blocks — reports, plans and technical answers
• Force Reply — folder paths and clarification prompts
• Reactions — low-clutter receipt and completion state
• Ephemeral — private hints inside a group

Try the colored buttons below. Each copies a harmless value."""


def styled_copy_button(text: str, value: str, style: str) -> dict[str, Any]:
    """Build a side-effect-free button that demonstrates Telegram styling."""
    if style not in {"primary", "success", "danger"}:
        raise ValueError("Telegram button style is invalid.")
    return {
        "text": str(text),
        "style": style,
        "copy_text": {"text": str(value)},
    }


def showcase_keyboard() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                styled_copy_button("Primary", "primary action", "primary"),
                styled_copy_button("Success", "successful action", "success"),
                styled_copy_button("Danger", "destructive action", "danger"),
            ]
        ]
    }


def force_reply_markup(
    placeholder: str,
    *,
    selective: bool = True,
) -> dict[str, Any]:
    value = " ".join(str(placeholder).split())
    if not 1 <= len(value) <= 64:
        raise ValueError("Telegram input placeholders must contain 1-64 characters.")
    return {
        "force_reply": True,
        "input_field_placeholder": value,
        "selective": bool(selective),
    }


def request_group_keyboard() -> dict[str, Any]:
    """Build the private-chat-only picker for an existing forum group."""
    required_rights = {
        "is_anonymous": False,
        "can_manage_chat": True,
        "can_delete_messages": True,
        "can_manage_video_chats": False,
        "can_restrict_members": False,
        "can_promote_members": False,
        "can_change_info": True,
        "can_invite_users": False,
        "can_post_stories": False,
        "can_edit_stories": False,
        "can_delete_stories": False,
        "can_manage_topics": True,
    }
    return {
        "keyboard": [
            [
                {
                    "text": "Choose an existing group",
                    "request_chat": {
                        "request_id": 1,
                        "chat_is_channel": False,
                        "chat_is_forum": True,
                        "bot_is_member": False,
                        "request_title": True,
                        "user_administrator_rights": dict(required_rights),
                        "bot_administrator_rights": dict(required_rights),
                    },
                }
            ]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True,
        "input_field_placeholder": "Choose a forum group",
        "selective": True,
    }


def rich_showcase_params(
    chat_id: int,
    message_thread_id: Optional[int],
) -> dict[str, Any]:
    return {
        "chat_id": int(chat_id),
        "message_thread_id": message_thread_id,
        "rich_message": {"html": SHOWCASE_RICH_HTML},
        "reply_markup": showcase_keyboard(),
    }


def fallback_showcase_params(
    chat_id: int,
    message_thread_id: Optional[int],
) -> dict[str, Any]:
    return {
        "chat_id": int(chat_id),
        "message_thread_id": message_thread_id,
        "text": SHOWCASE_FALLBACK_HTML,
        "parse_mode": "HTML",
        "reply_markup": showcase_keyboard(),
    }


def ephemeral_hint_params(
    chat_id: int,
    message_thread_id: Optional[int],
    receiver_user_id: int,
) -> dict[str, Any]:
    return {
        "chat_id": int(chat_id),
        "message_thread_id": message_thread_id,
        "receiver_user_id": int(receiver_user_id),
        "text": (
            "🎛 Control\n\n"
            "This is an ephemeral group hint. Only you and the bot should see "
            "it, and it will expire automatically."
        ),
    }


def force_reply_showcase_params(
    chat_id: int,
    message_thread_id: Optional[int],
) -> dict[str, Any]:
    return {
        "chat_id": int(chat_id),
        "message_thread_id": message_thread_id,
        "text": (
            "🎛 Control\n\n"
            "Force Reply puts the composer into a guided reply state. This is "
            "only a preview—dismiss it, or reply with any feedback about the UI."
        ),
        "reply_markup": force_reply_markup("Type UI feedback, or dismiss this reply"),
    }


def reaction_params(
    chat_id: int,
    message_id: int,
    emoji: str = "👀",
) -> dict[str, Any]:
    return {
        "chat_id": int(chat_id),
        "message_id": int(message_id),
        "reaction": [{"type": "emoji", "emoji": str(emoji)}],
        "is_big": False,
    }


def send_showcase(
    token: str,
    *,
    chat_id: int,
    message_thread_id: Optional[int],
    receiver_user_id: Optional[int] = None,
    source_message_id: Optional[int] = None,
    api_call: Callable[..., Any] = telegram_bridge.api_call,
) -> dict[str, Any]:
    """Send a live showcase, degrading individual new surfaces independently."""
    result: dict[str, Any] = {
        "rich_message": False,
        "styled_buttons": False,
        "reaction": False,
        "ephemeral_hint": False,
        "force_reply": False,
        "chat_picker": False,
    }
    try:
        rich_result = api_call(
            token,
            "sendRichMessage",
            **rich_showcase_params(chat_id, message_thread_id),
        )
        result["rich_message"] = True
        result["styled_buttons"] = True
    except telegram_bridge.BridgeError:
        rich_result = api_call(
            token,
            "sendMessage",
            **fallback_showcase_params(chat_id, message_thread_id),
        )
        result["styled_buttons"] = True

    reaction_message_id = source_message_id
    if reaction_message_id is None and isinstance(rich_result, dict):
        try:
            reaction_message_id = int(rich_result["message_id"])
        except (KeyError, TypeError, ValueError):
            reaction_message_id = None
    if reaction_message_id is not None:
        try:
            api_call(
                token,
                "setMessageReaction",
                **reaction_params(chat_id, reaction_message_id),
            )
            result["reaction"] = True
        except telegram_bridge.BridgeError:
            pass

    if receiver_user_id is not None and chat_id < 0:
        try:
            api_call(
                token,
                "sendMessage",
                **ephemeral_hint_params(
                    chat_id,
                    message_thread_id,
                    receiver_user_id,
                ),
            )
            result["ephemeral_hint"] = True
        except telegram_bridge.BridgeError:
            pass
    elif chat_id > 0:
        try:
            api_call(
                token,
                "sendMessage",
                chat_id=chat_id,
                text=(
                    "🎛 Control\n\n"
                    "This private-chat-only keyboard opens Telegram’s native "
                    "picker for an existing forum group."
                ),
                reply_markup=request_group_keyboard(),
            )
            result["chat_picker"] = True
        except telegram_bridge.BridgeError:
            pass

    api_call(
        token,
        "sendMessage",
        **force_reply_showcase_params(chat_id, message_thread_id),
    )
    result["force_reply"] = True
    return result

"""Provider-neutral guidance injected into every managed turn.

Managed turns are one-shot: the provider process is torn down once the turn
produces its reply. Agents repeatedly learn this the hard way by starting
background subagents or background shell jobs and promising to report back
when they finish — a promise that cannot be kept, because the notification
has nowhere to arrive. On 2026-07-25 that cost three Codex reviews and a
research agent, each killed mid-work with nothing usable written down.

Managed turns can also resume sessions that started in a local terminal. The
session history then describes an origin that is no longer authoritative:
Telegram Control supplies fresh scoped credentials to the resumed process.
Without an explicit handoff, a model can incorrectly reject Telegram
capabilities even while those capabilities are available.

The text lives here, provider-neutral, because *how* it reaches the model is
an adapter concern: Claude takes an appended system prompt and Codex takes
developer instructions on thread start or resume. Adapters declare whether
they can deliver it via `ProviderCapabilities.turn_guidance`, so a provider
that cannot is visibly unsupported rather than quietly ignored.

Keep this short. It is prepended to every turn on every topic, and a long
preamble buys attention from the actual task.
"""

from __future__ import annotations

from typing import Any, Optional

import app_config

TURN_GUIDANCE = (
    "You are currently running inside a Telegram Control-managed turn, even "
    "if this session originally started or previously ran in a local terminal. "
    "The TELEGRAM_CONTROL_* environment variables make the current process, "
    "not the session's origin or prior conversation, authoritative about "
    "Telegram capability availability. When the user requests a Telegram "
    "voice note, separate text update, or current-group icon, use the matching "
    "installed Telegram skill/helper; do not reject the request based on the "
    "session's history. Never start background agents or background shell "
    "jobs. This turn's "
    "process is torn down once you reply, so anything still running is killed "
    "mid-work and its results are lost. Run short work in the foreground, and "
    "put work that must outlive this turn in a detached tmux session."
)


def effective_turn_guidance(
    workspace_path: Optional[str],
    bridge_config: Optional[dict[str, Any]] = None,
) -> str:
    """Append user style/context after the non-replaceable core contract."""
    settings = (
        app_config.effective_settings(bridge_config, workspace_path)
        if bridge_config is not None
        else app_config.installed_settings(workspace_path)
    )
    addition = app_config.prompt_addition(settings, workspace_path)
    return TURN_GUIDANCE if not addition else f"{TURN_GUIDANCE}\n\n{addition}"

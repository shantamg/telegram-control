"""Provider-neutral guidance injected into every managed turn.

Managed turns are one-shot: the provider process is torn down once the turn
produces its reply. Agents repeatedly learn this the hard way by starting
background subagents or background shell jobs and promising to report back
when they finish — a promise that cannot be kept, because the notification
has nowhere to arrive. On 2026-07-25 that cost three Codex reviews and a
research agent, each killed mid-work with nothing usable written down.

The text lives here, provider-neutral, because *how* it reaches the model is
an adapter concern: Claude takes an appended system prompt, another provider
may take a config override or a prepended message. Adapters declare whether
they can deliver it via `ProviderCapabilities.turn_guidance`, so a provider
that cannot is visibly unsupported rather than quietly ignored.

Keep this short. It is prepended to every turn on every topic, and a long
preamble buys attention from the actual task.
"""

from __future__ import annotations

TURN_GUIDANCE = (
    "Never start background agents or background shell jobs. This turn's "
    "process is torn down once you reply, so anything still running is killed "
    "mid-work and its results are lost. Run short work in the foreground, and "
    "put work that must outlive this turn in a detached tmux session."
)

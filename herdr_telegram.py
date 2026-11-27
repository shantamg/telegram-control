#!/usr/bin/python3
"""Link a Telegram Control topic to a live Herdr agent."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import subprocess
import sys
import time

import detached_worker
import helper_paths
import telegram_bridge as bridge
import telegram_formatting
import voice_responses
from durable_store import DurableStore, HerdrVoiceStage, StoreError, SurfaceBinding


TARGET_TYPE = "herdr"
RELAY_CONFIG = helper_paths.CONFIG_DIR / "herdr-relay.json"
SSH_ALIAS = re.compile(r"[a-z][a-z0-9-]{0,60}")
VOICE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SCRIPT = Path(__file__).resolve()
HC = helper_paths.resolve_binary(
    "herdr_core_binary",
    Path.home() / ".local" / "bin" / "hc",
    Path("/opt/homebrew/bin/hc"),
    Path("/usr/local/bin/hc"),
    command_name="hc",
)
ADDRESS = re.compile(r"t[0-9a-z]+")
TICKET = re.compile(r"([1-9][0-9]*):([a-f0-9]{32})")
AGENT_PROGRAMS = {"claude-code": "claude"}
STALE_LINK = "The linked Herdr agent changed or closed. Ask the running agent to continue in Telegram again."
SESSION_ID = re.compile(r"[0-9a-fA-F-]{36}")


# Agents accept typed input mid-turn (Claude Code and Codex queue it), so a
# working agent can take a Telegram prompt now. Anything else (a permission
# dialog, an error) must not receive keystrokes; the inbox retries later.
DELIVERABLE_STATUSES = {"idle", "done", "working"}


class HerdrBusy(StoreError):
    """The agent cannot take input yet; the durable inbox should retry quietly."""


@dataclass(frozen=True)
class Terminal:
    address: str
    target_id: str
    cwd: Path
    status: str


def hc(*args: str, timeout: int = 8) -> dict:
    environment = os.environ.copy()
    environment.pop("HC_TERMINAL_ID", None)
    try:
        result = subprocess.run(
            [str(HC), *args, "--json"],
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        payload = json.loads(result.stdout if result.returncode == 0 else result.stderr)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise StoreError("Could not confirm the result from Herdr Core.") from None
    if not isinstance(payload, dict):
        raise StoreError("Herdr Core returned an invalid result.")
    if result.returncode != 0:
        kind = (payload.get("error") or {}).get("kind")
        if kind in {"not_found", "finished", "needs_target"}:
            raise StoreError(STALE_LINK)
        if kind in {"blocked", "status_mismatch"}:
            raise HerdrBusy("The Herdr agent is not ready for input (waiting in its pane).")
        raise StoreError("Could not confirm the result from Herdr Core.")
    return payload


def agent_process(root: int, app: str, *, caller: int | None) -> int:
    """Find the live agent process even when its foreground tool is a child."""
    commands = subprocess.check_output(
        ["/bin/ps", "-axo", "pid=,ppid=,comm="], text=True, timeout=5
    )
    processes = {}
    for line in commands.splitlines():
        fields = line.split(maxsplit=2)
        if len(fields) == 3:
            pid, parent, command = fields
            processes[int(pid)] = (int(parent), Path(command).name)

    def parents(pid: int) -> list[int]:
        lineage = []
        while pid > 1 and pid in processes and pid not in lineage:
            lineage.append(pid)
            pid = processes[pid][0]
        return lineage

    program = AGENT_PROGRAMS.get(app, app)
    matches = []
    for pid, (_, command) in processes.items():
        if command == program:
            lineage = parents(pid)
            if root in lineage:
                matches.append((lineage.index(root), pid))
    matches.sort()
    if not matches or (len(matches) > 1 and matches[0][0] == matches[1][0]):
        raise StoreError("Could not identify a single running agent in this Herdr terminal.")
    pid = matches[0][1]
    if caller is not None and pid not in parents(caller):
        raise StoreError("Run this command from the agent inside the Herdr terminal.")
    return pid


def provider_session_id(pid: int, app: str, labels: dict) -> str | None:
    """Recognize an exact provider ID when the process publishes one."""
    provider = AGENT_PROGRAMS.get(app, app)
    label = labels.get(f"{provider}.session", "")
    if isinstance(label, str) and label.startswith(f"{pid}:"):
        return label.split(":", 1)[1]
    if provider == "claude":
        try:
            session = json.loads(
                (Path.home() / ".claude" / "sessions" / f"{pid}.json").read_text()
            )
            if isinstance(session, dict) and session.get("pid") == pid:
                value = session.get("sessionId")
                if isinstance(value, str) and SESSION_ID.fullmatch(value):
                    return value
        except (OSError, ValueError):
            pass
    if provider == "codex":
        try:
            result = subprocess.run(
                ["lsof", "-a", "-p", str(pid), "-Fn"],
                capture_output=True, text=True, timeout=4,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        # An open rollout is tied to this exact process. Sorting files in
        # the workspace by recency would conflate agents working in parallel.
        ids = {
            match[1] for line in result.stdout.splitlines()
            if (match := re.search(r"/rollout-[^/]*-([0-9a-fA-F-]{36})\.jsonl$", line))
        }
        if len(ids) == 1:
            return ids.pop()
    return None


def current_terminal_address() -> str:
    """This process's Herdr terminal, even when the agent scrubbed HC_* variables.

    Codex runs tool commands with a filtered environment, so HC_TERMINAL_ID may
    be absent. Fall back to the live terminal whose root process is one of our
    ancestors; inspect_terminal still verifies the agent lineage afterwards.
    """
    address = os.environ.get("HC_TERMINAL_ID", "")
    if ADDRESS.fullmatch(address):
        return address
    commands = subprocess.check_output(
        ["/bin/ps", "-axo", "pid=,ppid="], text=True, timeout=5
    )
    parent = {}
    for line in commands.splitlines():
        fields = line.split()
        if len(fields) == 2:
            parent[int(fields[0])] = int(fields[1])
    ancestors, pid = set(), os.getpid()
    while pid > 1 and pid not in ancestors:
        ancestors.add(pid)
        pid = parent.get(pid, 0)
    matches = []
    for row in hc("ls").get("terminals", []):
        candidate = row.get("address", "")
        if row.get("state") != "live" or not ADDRESS.fullmatch(candidate):
            continue
        detail = hc("get", candidate)
        if int(detail.get("pid") or 0) in ancestors:
            matches.append(candidate)
    if len(matches) != 1:
        raise StoreError("Run this from an agent in a local Herdr terminal.")
    return matches[0]


def inspect_terminal(address: str, *, from_agent: bool = False) -> Terminal:
    if not ADDRESS.fullmatch(address):
        raise StoreError("Run this from an agent in a local Herdr terminal.")
    row = hc("get", address)
    status = row.get("status") or {}
    if row.get("state") != "live" or row.get("address") != address:
        raise StoreError(STALE_LINK)
    if not status.get("app") or not row.get("pid") or not row.get("created_ms"):
        raise StoreError("Herdr has not identified an agent in this terminal yet.")
    pid = agent_process(
        int(row["pid"]), status["app"], caller=os.getpid() if from_agent else None
    )
    started = subprocess.check_output(
        ["/bin/ps", "-p", str(pid), "-o", "lstart="],
        env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
        text=True,
        timeout=5,
    ).strip()
    core_id = (hc("server", "status").get("core") or {}).get("id")
    if not core_id or not started:
        raise StoreError("Could not identify the running Herdr agent.")
    identity = [
        core_id, address, row["created_ms"], pid, started, status["app"],
        provider_session_id(pid, status["app"], row.get("labels") or {}),
    ]
    digest = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    cwd = (row.get("foreground") or {}).get("cwd") or row.get("cwd")
    if not cwd or not Path(cwd).is_absolute():
        raise StoreError("The Herdr terminal's working directory is unavailable.")
    return Terminal(address, f"{address}:{digest}", Path(cwd), status["state"])


def relay_configuration() -> dict:
    try:
        return json.loads(RELAY_CONFIG.read_text())
    except FileNotFoundError:
        return {}


def relay_call(alias: str, request: dict, timeout: int = 120) -> dict:
    if not SSH_ALIAS.fullmatch(alias):
        raise StoreError("Invalid Herdr relay SSH alias.")
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", alias, "telegram-control"],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        raise StoreError("The Herdr relay did not respond.") from None
    if not isinstance(payload, dict):
        raise StoreError("The Herdr relay returned an invalid response.")
    if result.returncode != 0:
        raise StoreError(payload.get("error", "The Herdr relay failed."))
    return payload


def peer_terminal(machine: str, address: str) -> Terminal:
    peers = relay_configuration().get("peers", {})
    if machine not in peers:
        raise StoreError(STALE_LINK)
    row = relay_call(peers[machine]["ssh"], {"action": "inspect", "address": address}, timeout=20)
    if row["address"] != address or not row["target_id"].startswith(f"{address}:"):
        raise StoreError(STALE_LINK)
    return Terminal(f"{machine}/{address}", f"{machine}/{row['target_id']}", Path(row["cwd"]), row["status"])


def linked_terminal(binding: SurfaceBinding) -> Terminal:
    try:
        address = binding.target_id.split(":", 1)[0]
        machine, separator, remote_address = address.partition("/")
        terminal = peer_terminal(machine, remote_address) if separator else inspect_terminal(address)
    except (OSError, subprocess.SubprocessError):
        raise StoreError(STALE_LINK) from None
    if terminal.target_id != binding.target_id:
        raise StoreError(STALE_LINK)
    return terminal


def git_common_dir(path: Path) -> str | None:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def select_group(store: DurableStore, cwd: Path, name: str | None) -> int:
    workspaces = store.connection.execute(
        """
        SELECT w.* FROM forum_workspaces AS w
        JOIN surface_bindings AS root ON root.binding_id = w.forum_binding_id
        WHERE w.state = 'active' AND root.state = 'active'
            AND root.chat_id = w.chat_id AND root.message_thread_id = 0
            AND root.target_type = 'controller' AND root.target_id = 'control'
        ORDER BY w.display_name, w.chat_id
        """
    ).fetchall()
    if name:
        matches = [row for row in workspaces if name.casefold() == row["display_name"].casefold()]
    else:
        cwd = cwd.resolve()
        containing = []
        for row in workspaces:
            project = Path(row["project_path"]).resolve()
            if project == cwd or project in cwd.parents:
                containing.append((len(project.parts), row))
        if containing:
            depth = max(depth for depth, _ in containing)
            matches = [row for size, row in containing if size == depth]
        else:
            common = git_common_dir(cwd)
            matches = [
                row for row in workspaces
                if common and git_common_dir(Path(row["project_path"])) == common
            ]
    if len(matches) == 1:
        return int(matches[0]["chat_id"])
    choices = ", ".join(row["display_name"] for row in matches or workspaces)
    if not choices:
        raise StoreError("Bind a private project group in Telegram Control before linking this Herdr agent.")
    reason = "Several private groups match." if matches else "No private group matches this workspace."
    raise StoreError(f"{reason} Available groups: {choices}. Choose an exact group name with --group.")


def read_state(store: DurableStore, key: str) -> dict | None:
    row = store.connection.execute(
        "SELECT value FROM controller_state WHERE key = ?", (key,)
    ).fetchone()
    return json.loads(row["value"]) if row is not None else None


def reserve_state(store: DurableStore, key: str, value: dict) -> bool:
    return store.connection.execute(
        "INSERT INTO controller_state (key, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO NOTHING",
        (key, json.dumps(value), time.time()),
    ).rowcount == 1


def write_state(store: DurableStore, key: str, value: dict) -> None:
    store.connection.execute(
        "UPDATE controller_state SET value = ?, updated_at = ? WHERE key = ?",
        (json.dumps(value), time.time(), key),
    )


def find_linked_topic(store: DurableStore, terminal: Terminal) -> SurfaceBinding | None:
    rows = store.connection.execute(
        """
        SELECT binding_id FROM surface_bindings
        WHERE target_type = ? AND target_id = ? AND state = 'active'
        LIMIT 2
        """,
        (TARGET_TYPE, terminal.target_id),
    ).fetchall()
    if len(rows) > 1:
        raise StoreError("More than one Telegram topic is linked to this Herdr agent.")
    return store.resolve_surface_binding_by_id(rows[0]["binding_id"]) if rows else None


def link_topic(
    store: DurableStore, terminal: Terminal, title: str, group: str | None
) -> SurfaceBinding:
    name = " ".join(title.split())
    if not name or len(name) > 128:
        raise StoreError("Choose a Telegram topic name of 1 to 128 characters.")
    existing = find_linked_topic(store, terminal)
    if existing is not None:
        return existing

    key = f"herdr-topic:{terminal.target_id}"
    state = read_state(store, key)
    if state is not None and state.get("linked"):
        # The old topic was retired by the controller after Telegram deleted it.
        store.connection.execute(
            "DELETE FROM controller_state WHERE key = ? AND value = ?",
            (key, json.dumps(state)),
        )
        state = None
    if state is None:
        chat_id = select_group(store, terminal.cwd, group)
        token = bridge.read_token()
        chat = bridge.api_call(token, "getChat", chat_id=chat_id)
        if (
            chat.get("type") != "supergroup"
            or not chat.get("is_forum")
            or chat.get("username")
            or chat.get("active_usernames")
        ):
            raise StoreError("Herdr handoffs require a private Telegram forum group.")
        state = {"chat_id": chat_id, "name": name}
        if not reserve_state(store, key, state):
            raise StoreError("Another request is creating this Herdr topic. Try opening the link again shortly.")
        try:
            topic = bridge.api_call(token, "createForumTopic", chat_id=chat_id, name=name)
        except bridge.BridgeError as exc:
            if not bridge.is_retryable_telegram_error(exc):
                store.connection.execute("DELETE FROM controller_state WHERE key = ?", (key,))
            raise
        thread_id = int(topic["message_thread_id"])
        if thread_id <= 1:
            raise StoreError("Telegram returned an invalid topic ID.")
        state["thread_id"] = thread_id
        write_state(store, key, state)
    if "thread_id" not in state:
        raise StoreError("The previous topic creation has an unknown result. Check the project group before attempting another handoff.")

    chat_id, thread_id = state["chat_id"], state["thread_id"]
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        if store.resolve_forum_workspace(chat_id) is None:
            raise StoreError("This Telegram project group is no longer bound.")
        binding = store.ensure_surface_binding(
            chat_id, "task", state["name"], TARGET_TYPE, terminal.target_id, thread_id
        )
        store.enqueue_api_call(
            f"herdr-topic:{binding.binding_id}:intro",
            "sendMessage",
            {
                "chat_id": chat_id,
                "message_thread_id": thread_id,
                "text": "This topic is linked to your running Herdr agent. Send a message or a voice note to continue the conversation.",
            },
        )
        state["linked"] = True
        write_state(store, key, state)
        store.connection.commit()
    except BaseException:
        store.connection.rollback()
        raise
    return binding


def tear_down_topic(store: DurableStore, binding: SurfaceBinding) -> None:
    """Revoke the route and queue Telegram deletion as one operation."""
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        updated = store.connection.execute(
            """
            UPDATE surface_bindings SET state = 'revoked', updated_at = ?
            WHERE binding_id = ? AND target_type = ? AND target_id = ? AND state = 'active'
            """,
            (time.time(), binding.binding_id, TARGET_TYPE, binding.target_id),
        )
        if updated.rowcount:
            store.enqueue_api_call(
                f"herdr-topic:{binding.binding_id}:delete",
                "deleteForumTopic",
                {"chat_id": binding.chat_id, "message_thread_id": binding.message_thread_id},
            )
        store.connection.commit()
    except BaseException:
        store.connection.rollback()
        raise


def preview_topic_teardown(store: DurableStore, terminal: Terminal) -> dict:
    binding = find_linked_topic(store, terminal)
    if binding is None:
        raise StoreError("This Herdr agent has no active Telegram topic.")
    return {
        "topic": binding.display_name,
        "url": detached_worker.telegram_topic_url(binding),
        "confirmation_id": binding.binding_id,
    }


def confirm_topic_teardown(store: DurableStore, terminal: Terminal, binding_id: int) -> str:
    binding = store.resolve_surface_binding_by_id(binding_id)
    operation = f"herdr-topic:{binding_id}:delete"
    if binding is None:
        # A repeated confirmation can check the same operation after revocation.
        previous = store.connection.execute(
            """
            SELECT 1 FROM surface_bindings
            WHERE binding_id = ? AND target_type = ? AND target_id = ? AND state = 'revoked'
            """,
            (binding_id, TARGET_TYPE, terminal.target_id),
        ).fetchone()
        state = store.outbox_operation_state(operation) if previous is not None else None
        if state is not None:
            return state
    if (
        binding is None or binding.target_type != TARGET_TYPE
        or binding.target_id != terminal.target_id
    ):
        raise StoreError("That confirmation is for a different or inactive topic.")
    tear_down_topic(store, binding)
    state = store.outbox_operation_state(operation)
    if state is None:
        raise StoreError("The Telegram topic changed before deletion was queued.")
    return state


def forward_input(
    store: DurableStore,
    binding: SurfaceBinding,
    job_id: int,
    message_id: int,
    text: str,
    *,
    voice: bool,
) -> None:
    key = f"herdr-turn:{job_id}"
    if read_state(store, key) is not None:
        return
    if (
        store.resolve_surface_binding_by_id(binding.binding_id) != binding
        or store.resolve_forum_workspace(binding.chat_id) is None
    ):
        raise StoreError(STALE_LINK)
    terminal = linked_terminal(binding)
    if terminal.status not in DELIVERABLE_STATUSES:
        raise HerdrBusy(f"The Herdr agent is not ready for input ({terminal.status or 'unknown'}).")
    nonce = secrets.token_hex(16)
    state = {
        "binding_id": binding.binding_id,
        "nonce": nonce,
        "voice": voice,
        "message_id": message_id,
        "created_at": time.time(),
    }
    if not reserve_state(store, key, state):
        return
    machine, separator, address = terminal.address.partition("/")
    peer = relay_configuration()["peers"][machine] if separator else None
    script = peer["script"] if peer else str(SCRIPT)
    command = shlex.join(["/usr/bin/python3", script, "reply", f"{job_id}:{nonce}"])
    prompt = (
        f"{text}\n\n[Telegram Control handoff]\n"
        "The user sent this from the Telegram topic linked to your Herdr session. "
        "When you finish this request (or need the user's answer), use your terminal tool to "
        f"run {command} and pass your final user-facing reply on stdin. "
        "Then give the same reply normally here. Never include the reply command or ticket in your answer. "
        "The reply command is scoped to this particular message."
    )
    try:
        if peer:
            relay_call(peer["ssh"], {
                "action": "deliver",
                "address": address,
                "target_id": terminal.target_id.split("/", 1)[1],
                "prompt": prompt,
            })
        else:
            deliver_prompt(terminal, prompt)
    except HerdrBusy:
        # Nothing reached the pane, so release the turn for the next attempt.
        store.connection.execute("DELETE FROM controller_state WHERE key = ?", (key,))
        raise
    except StoreError as exc:
        # The relay flattens errors to text; recognise the peer's busy refusal.
        if peer and "not ready for input" in str(exc):
            store.connection.execute("DELETE FROM controller_state WHERE key = ?", (key,))
            raise HerdrBusy(str(exc)) from None
        raise


def deliver_prompt(terminal: Terminal, prompt: str) -> None:
    if terminal.status not in DELIVERABLE_STATUSES:
        raise HerdrBusy(f"The Herdr agent is not ready for input ({terminal.status or 'unknown'}).")
    args = ["send"]
    if terminal.status == "idle":
        args += ["--if", "idle"]
    hc(*args, terminal.address, prompt, timeout=85)


def queue_reply(store: DurableStore, ticket: str, text: str, terminal: Terminal) -> None:
    match = TICKET.fullmatch(ticket)
    if match is None:
        raise StoreError("This Telegram reply ticket is invalid.")
    job_id, nonce = match.groups()
    state = read_state(store, f"herdr-turn:{job_id}")
    if state is None or not secrets.compare_digest(nonce, state["nonce"]):
        raise StoreError("This Telegram reply ticket is invalid.")
    binding = store.resolve_surface_binding_by_id(state["binding_id"])
    if (
        binding is None or binding.state != "active" or binding.target_type != TARGET_TYPE
        or binding.target_id != terminal.target_id
        or store.resolve_forum_workspace(binding.chat_id) is None
    ):
        raise StoreError(STALE_LINK)
    if time.time() > state["created_at"] + 24 * 60 * 60:
        raise StoreError("This Telegram reply ticket expired.")
    prefix = f"herdr-turn:{job_id}:reply"
    if state.get("reply_queued") or store.outbox_operation_state(f"{prefix}:text:0") is not None:
        return
    scope = {
        "chat_id": binding.chat_id,
        "message_thread_id": binding.message_thread_id,
        "reply_parameters": {"message_id": state["message_id"], "allow_sending_without_reply": True},
    }
    voice_path = None
    if state["voice"]:
        config = store.voice_configuration()
        try:
            voice_path = voice_responses.synthesize_voice(
                text,
                f"herdr-{job_id}-{hashlib.sha256(text.encode()).hexdigest()[:12]}",
                protected_paths=store.pending_voice_file_paths(),
                voice_name=config.voice_name,
                rate=config.rate,
            )
        except (OSError, subprocess.SubprocessError, voice_responses.VoiceResponseError):
            text += "\n\nVoice playback was unavailable for this reply."
    store.connection.execute("BEGIN IMMEDIATE")
    try:
        if store.resolve_surface_binding_by_id(binding.binding_id) != binding:
            raise StoreError(STALE_LINK)
        state = read_state(store, f"herdr-turn:{job_id}")
        if not state.get("reply_queued") and store.outbox_operation_state(f"{prefix}:text:0") is None:
            edit_receipt = state["voice"] and store.outbox_operation_state(f"herdr-turn:{job_id}:receipt")
            state["reply_queued"] = True
            write_state(store, f"herdr-turn:{job_id}", state)
            chunks = telegram_formatting.render_plain_chunks(text, speaker="", limit=3800)
            for index, chunk in enumerate(chunks):
                if index == 0 and edit_receipt:
                    store.stage_herdr_voice_edit(int(job_id), HerdrVoiceStage.ANSWERED, {"text": chunk.text})
                    continue
                params = dict(scope)
                chunk.add_to_params(params)
                store.enqueue_api_call(f"{prefix}:text:{index}", "sendMessage", params, serialize_key=prefix)
            if voice_path is not None:
                store.enqueue_api_call(
                    f"{prefix}:voice",
                    "sendVoice",
                    {**scope, "__voice_file_path": str(voice_path)},
                    serialize_key=prefix,
                )
        store.connection.commit()
    except BaseException:
        store.connection.rollback()
        raise


def queue_voice(store: DurableStore, terminal: Terminal, key: str, text: str) -> str:
    if not VOICE_KEY.fullmatch(key):
        raise StoreError("Use a lowercase voice key of up to 64 letters, digits or hyphens.")
    if not text or len(text) > voice_responses.MAX_SPEECH_CHARACTERS:
        raise StoreError("Pass 1 to 3,500 characters of voice text on stdin.")
    binding = find_linked_topic(store, terminal)
    if binding is None or store.resolve_forum_workspace(binding.chat_id) is None:
        raise StoreError("Link this Herdr agent to a Telegram topic first.")
    operation = f"herdr-voice:{binding.binding_id}:{key}"
    state = store.outbox_operation_state(operation)
    if state is not None:
        return state
    config = store.voice_configuration()
    audio_key = hashlib.sha256(operation.encode()).hexdigest()[:32]
    voice_path = voice_responses.synthesize_voice(
        text,
        audio_key,
        protected_paths=store.pending_voice_file_paths(),
        voice_name=config.voice_name,
        rate=config.rate,
    )
    with store.connection:
        if store.resolve_surface_binding_by_id(binding.binding_id) != binding:
            raise StoreError(STALE_LINK)
        store.enqueue_api_call(operation, "sendVoice", {
            "chat_id": binding.chat_id,
            "message_thread_id": binding.message_thread_id,
            "__voice_file_path": str(voice_path),
        })
    return store.outbox_operation_state(operation) or "queued"


def handle_action(store: DurableStore, terminal: Terminal, request: dict) -> dict:
    action = request["action"]
    if action == "link":
        binding = link_topic(store, terminal, request["name"], request.get("group"))
        return {"topic": binding.display_name, "url": detached_worker.telegram_topic_url(binding)}
    if action == "reply":
        answer = request["text"]
        if not answer or len(answer) > 40_000:
            raise StoreError("Pass 1 to 40,000 characters of reply text on stdin.")
        queue_reply(store, request["ticket"], answer, terminal)
        return {"reply": "queued"}
    if action == "voice":
        return {"voice": queue_voice(store, terminal, request["key"], request["text"])}
    if action != "teardown":
        raise StoreError("Unsupported Herdr action.")
    confirmation = request.get("confirm")
    if confirmation is None:
        return preview_topic_teardown(store, terminal)
    state = confirm_topic_teardown(store, terminal, confirmation)
    deadline = time.monotonic() + 8
    while state in {"queued", "leased"} and time.monotonic() < deadline:
        time.sleep(0.25)
        state = store.outbox_operation_state(f"herdr-topic:{confirmation}:delete")
    return {"deletion": state}


def serve_relay(peer: str | None) -> dict:
    if os.environ.get("SSH_ORIGINAL_COMMAND") != "telegram-control":
        raise StoreError("The Herdr relay requires its dedicated SSH command.")
    request = json.loads(sys.stdin.read(150_000))
    action, address = request["action"], request["address"]
    if peer is None:
        terminal = inspect_terminal(address)
        if action == "inspect":
            return {
                "address": terminal.address,
                "target_id": terminal.target_id,
                "cwd": str(terminal.cwd),
                "status": terminal.status,
            }
        if action != "deliver" or terminal.target_id != request.get("target_id"):
            raise StoreError(STALE_LINK)
        deliver_prompt(terminal, request["prompt"])
        return {"delivery": "sent"}
    terminal = peer_terminal(peer, address)
    if terminal.target_id != f"{peer}/{request['target_id']}":
        raise StoreError(STALE_LINK)
    if action == "link" and not request.get("group"):
        request["group"] = relay_configuration()["peers"][peer].get("group")
    with DurableStore(bridge.CONFIG_DIR / "controller.sqlite3") as store:
        return handle_action(store, terminal, request)


def dispatch_installed_script(script: Path, command: str) -> None:
    """Direct receiver and SSH entry points also follow the stable release link."""
    if os.environ.pop("HERDR_DISPATCHED_SCRIPT", None) == str(script.resolve()):
        return
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    deploy = data / "telegram-control"
    runner = deploy / "app/deploy/herdr-run.py"
    root = script.resolve().parent
    if runner.is_file() and (root == (deploy / "app").resolve() or
                             (root.parent == deploy and root.name.startswith("app-"))):
        os.execv(sys.executable, [sys.executable, str(runner), command, *sys.argv[1:]])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    link = commands.add_parser("link", help="Create or reopen this Herdr agent's Telegram topic.")
    link.add_argument("--name", required=True)
    link.add_argument("--group")
    reply = commands.add_parser("reply", help="Post the response for one linked Telegram turn.")
    reply.add_argument("ticket")
    voice = commands.add_parser("voice", help="Send a voice note to this Herdr agent's linked topic.")
    voice.add_argument("--key", required=True)
    teardown = commands.add_parser("teardown", help="Preview or confirm removal of this agent's linked Telegram topic.")
    teardown.add_argument("--confirm", type=int, metavar="CONFIRMATION_ID")
    relay = commands.add_parser("relay", help="Process a request via a dedicated SSH relay key.")
    relay.add_argument("--peer")
    args = parser.parse_args()
    try:
        if args.command == "relay":
            print(json.dumps(serve_relay(args.peer)))
            return 0
        terminal = inspect_terminal(current_terminal_address(), from_agent=True)
        request = {key: value for key, value in vars(args).items() if key != "command"}
        request["action"] = args.command
        if args.command in {"reply", "voice"}:
            request["text"] = sys.stdin.read(40_001).strip()
        hub = relay_configuration().get("hub")
        if hub:
            request.update(address=terminal.address, target_id=terminal.target_id)
            result = relay_call(hub, request, timeout=220)
        else:
            with DurableStore(bridge.CONFIG_DIR / "controller.sqlite3") as store:
                result = handle_action(store, terminal, request)
        print(json.dumps(result))
        return 0
    except (OSError, KeyError, ValueError, subprocess.SubprocessError, StoreError, bridge.BridgeError, voice_responses.VoiceResponseError) as exc:
        if args.command == "relay":
            print(json.dumps({"error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    dispatch_installed_script(Path(__file__), "relay-helper")
    raise SystemExit(main())

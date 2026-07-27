#!/usr/bin/python3
"""Stage 1 durable Telegram collector, inbox worker, and outbox sender."""

from __future__ import annotations

import argparse
from dataclasses import replace
import fcntl
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Optional

import app_config
import detached_worker
import discovery
import provider_adapters
import provider_defaults
import router_contract
import telegram_bridge as bridge
import telegram_help
import tmux_console
import voice_responses
from durable_store import (
    SCHEMA_VERSION,
    AgentMailboxJob,
    DurableStore,
    InboxJob,
    LeaseLostError,
    OutboxMessage,
    RouterMailboxJob,
    StoreError,
    validate_provider_config,
)


DATABASE_PATH = bridge.CONFIG_DIR / "controller.sqlite3"
SCRIPT_PATH = Path(__file__).resolve()
SKILLS_SOURCE_DIR = SCRIPT_PATH.parent / "skills"
# Skill sources are checkout-relative: they refer to this repository through a
# placeholder that installation resolves to wherever the checkout actually is.
SKILL_ROOT_PLACEHOLDER = "{{TELEGRAM_CONTROL_ROOT}}"
SHARED_SKILLS_DIR = Path.home() / ".agents" / "skills"
CLAUDE_SKILLS_DIR = Path.home() / ".claude" / "skills"
MANAGED_SHARED_SKILLS = (
    "telegram-group-icon",
    "telegram-create-topic",
    "telegram-detached-worker",
    "telegram-topic-teardown",
    "telegram-text-update",
    "telegram-voice-message",
    "telegram-ask-owner",
)
DEFAULT_AGENT_WORKERS = 8
MAX_AGENT_WORKERS = 16
TOPIC_PROBE_INTERVAL_SECONDS = 24 * 60 * 60
TOPIC_PROBE_BATCH_SIZE = 100
TOPIC_PROBE_TEXT = "\u2063"
ROUTER_MAX_COMPLETED_TURNS = 12
ROUTER_MAX_INPUT_TOKENS = 180_000
ROUTER_MAX_DISCOVERY_STEPS = 6
ROUTER_MAX_DISCOVERY_REFS = 40
ROUTER_MAX_LOOP_SECONDS = 240.0
CONTROL_SPEAKER = "🎛 Control"
MISSING_TOPIC_ERROR_MARKERS = (
    "message thread not found",
    "topic_id_invalid",
    "topic id invalid",
)
RELOAD_JOB_PREFIX = "local.telegram-control.reload"
DEFAULT_RESTART_DELAY_SECONDS = 20.0
MAX_RESTART_DELAY_SECONDS = 5 * 60.0


def control_message(text: str) -> str:
    """Render a Control-authored Telegram message with its speaker label."""
    return f"{CONTROL_SPEAKER}\n\n{text}"


def handoff_message(project_name: str, text: str) -> str:
    """Render a visible Control → project handoff."""
    return f"{CONTROL_SPEAKER} → {project_name}\n\n{text}"


def log_event(kind: str, **details: Any) -> None:
    record = {"time": time.time(), "kind": kind}
    record.update(details)
    print(json.dumps(record, separators=(",", ":"), sort_keys=True), flush=True)


def process_name(role: str) -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{role}:{uuid.uuid4().hex[:8]}"


def open_store(path: Path) -> DurableStore:
    store = DurableStore(path)
    try:
        store.initialize_poll_offset(bridge.read_offset())
        check = store.quick_check()
        if check != "ok":
            raise StoreError(f"SQLite quick_check returned: {check}")
        return store
    except BaseException:
        store.close()
        raise


def collect_once(
    store: DurableStore,
    token: str,
    config: dict[str, Any],
    timeout: int = 50,
) -> int:
    updates = bridge.api_call(
        token,
        "getUpdates",
        offset=store.poll_offset(),
        timeout=timeout,
        allowed_updates=["message", "callback_query"],
    )
    for update in updates:
        authorization_failure = bridge.update_authorization_failure(
            config,
            update,
        )
        stored_update = (
            update
            if authorization_failure is None
            else {
                "update_id": int(update["update_id"]),
                "ignored_unauthorized": authorization_failure,
            }
        )
        inserted = store.ingest_update(stored_update)
        committed_offset = store.poll_offset()
        if committed_offset is not None:
            # Keep Stage 0's fallback cursor current, but only after the durable
            # transaction has committed. A failed mirror can cause a harmless
            # duplicate fetch; it cannot lose the durable job.
            bridge.save_offset(committed_offset)
        log_event(
            (
                "update_ignored"
                if inserted and authorization_failure is not None
                else "update_ingested"
                if inserted
                else "update_duplicate"
            ),
            update_id=int(update["update_id"]),
            offset=committed_offset,
            **(
                {"authorization_failure": authorization_failure}
                if authorization_failure is not None
                else {}
            ),
        )
    return len(updates)


def collect_command(args: argparse.Namespace) -> None:
    token = bridge.read_token()
    config = bridge.load_config()
    with open_store(args.db) as store:
        if args.once:
            collect_once(store, token, config, timeout=0)
            return

        delay = 1.0
        while True:
            try:
                collect_once(store, token, config)
                delay = 1.0
            except bridge.BridgeError as exc:
                log_event("collector_error", error=str(exc), retry_seconds=delay)
                time.sleep(delay)
                delay = min(60.0, delay * 2)


def process_inbox_job(
    store: DurableStore,
    config: dict[str, Any],
    job: InboxJob,
    worker_id: str,
) -> None:
    try:
        bridge.process_update(
            config,
            job.payload,
            extra_environment={
                "TELEGRAM_CONTROL_DB": str(store.path),
                "TELEGRAM_CONTROL_JOB_ID": str(job.job_id),
                "TELEGRAM_CONTROL_JOB_ATTEMPT": str(job.attempts),
            },
        )
        store.complete_job(job.job_id, worker_id)
        log_event(
            "inbox_succeeded",
            job_id=job.job_id,
            update_id=job.update_id,
            attempts=job.attempts,
        )
    except bridge.RetryableHandlerError as exc:
        state = store.fail_job(
            job.job_id,
            worker_id,
            str(exc),
            terminal_failure_text=control_message(
                "❌ I couldn’t download that voice message after several "
                "attempts. Please send it again."
            ),
        )
        log_event(
            "inbox_retryable_failure",
            job_id=job.job_id,
            update_id=job.update_id,
            attempts=job.attempts,
            state=state,
            error=str(exc),
        )
    except (bridge.BridgeError, OSError) as exc:
        state = store.fail_job(job.job_id, worker_id, str(exc))
        log_event(
            "inbox_failed",
            job_id=job.job_id,
            update_id=job.update_id,
            attempts=job.attempts,
            state=state,
            error=str(exc),
        )


def work_command(args: argparse.Namespace) -> None:
    config = bridge.load_config()
    worker_id = process_name("inbox")
    with open_store(args.db) as store:
        while True:
            job = store.claim_job(
                worker_id,
                lease_seconds=args.lease_seconds,
            )
            if job is None:
                if args.once:
                    return
                time.sleep(args.idle_sleep)
                continue
            process_inbox_job(store, config, job, worker_id)
            if args.once:
                return


def process_agent_mailbox_job(
    store: DurableStore,
    job: AgentMailboxJob,
    worker_id: str,
) -> None:
    try:
        agent = store.resolve_agent(job.agent_id)
        if agent is None:
            raise StoreError("Managed agent no longer exists.")
        if agent.project_path:
            # Launch-time revalidation: the stored paths must still resolve
            # to themselves (no symlink swap) with the working directory
            # contained in the confirmed workspace boundary.
            root_real, workdir_real, git_root = discovery.validate_agent_workspace(
                agent.project_path,
                agent.working_directory,
                agent.git_repository_root,
            )
            if root_real != agent.project_path or workdir_real != (
                agent.working_directory or agent.project_path
            ) or git_root != agent.git_repository_root:
                raise StoreError(
                    "Managed agent workspace paths no longer resolve to "
                    "their enrolled locations."
                )
        store.enqueue_agent_voice_status(
            job.source_inbox_job_id,
            "working",
            job.input_text,
        )
        agent = replace(
            agent,
            runtime_environment={
                "TELEGRAM_CONTROL_DB": str(store.path),
                "TELEGRAM_CONTROL_AGENT_ID": agent.agent_id,
                "TELEGRAM_CONTROL_MAILBOX_ID": str(job.mailbox_id),
                "TELEGRAM_CONTROL_WORKER_ID": worker_id,
            },
        )
        adapter = provider_adapters.adapter_for(agent)

        def on_progress(stage: str, detail: str) -> None:
            if stage == "turn_started":
                store.attach_agent_mailbox_turn(
                    job.mailbox_id,
                    worker_id,
                    detail,
                )
            else:
                store.update_agent_mailbox_progress(
                    job.mailbox_id,
                    worker_id,
                    stage,
                    detail=detail,
                )

        def poll_control() -> Optional[provider_adapters.ProviderControl]:
            control = store.claim_agent_turn_control(
                job.mailbox_id,
                worker_id,
            )
            if control is None:
                return None
            return provider_adapters.ProviderControl(
                control_id=control.control_id,
                kind=control.control_type,
                text=control.input_text,
                expected_turn_id=control.expected_turn_id,
            )

        def on_control(
            control: provider_adapters.ProviderControl,
            outcome: str,
            detail: str,
        ) -> None:
            store.finish_agent_turn_control(
                control.control_id,
                job.mailbox_id,
                worker_id,
                outcome,
                detail,
            )

        result = adapter.run_turn(
            agent,
            job.input_text,
            job.provider_session_id,
            on_session=lambda session_id: store.attach_agent_mailbox_session(
                job.mailbox_id,
                worker_id,
                session_id,
            ),
            heartbeat=lambda: store.heartbeat_agent_mailbox(
                job.mailbox_id,
                worker_id,
            ),
            on_progress=on_progress,
            poll_control=poll_control,
            on_control=on_control,
        )
        store.complete_agent_mailbox(
            job.mailbox_id,
            worker_id,
            result.provider_session_id,
            result.final_text,
            result.usage,
        )
        log_event(
            "agent_turn_succeeded",
            mailbox_id=job.mailbox_id,
            agent_id=job.agent_id,
            attempts=job.attempts,
            provider_session_id=result.provider_session_id,
        )
    except provider_adapters.ProviderTurnCancelled as exc:
        store.cancel_agent_mailbox(
            job.mailbox_id,
            worker_id,
            str(exc),
        )
        log_event(
            "agent_turn_cancelled",
            mailbox_id=job.mailbox_id,
            agent_id=job.agent_id,
            attempts=job.attempts,
        )
    except (provider_adapters.ProviderAdapterError, OSError, StoreError) as exc:
        state = store.fail_agent_mailbox(
            job.mailbox_id,
            worker_id,
            str(exc),
        )
        log_event(
            "agent_turn_failed",
            mailbox_id=job.mailbox_id,
            agent_id=job.agent_id,
            attempts=job.attempts,
            state=state,
            error=str(exc),
        )


def work_agents_command(args: argparse.Namespace) -> None:
    worker_id = process_name("agent")
    with open_store(args.db) as store:
        while True:
            job = store.claim_agent_mailbox(
                worker_id,
                lease_seconds=args.lease_seconds,
            )
            if job is None:
                if args.once:
                    return
                time.sleep(args.idle_sleep)
                continue
            process_agent_mailbox_job(store, job, worker_id)
            if args.once:
                return


def dispatch_preview_text(
    store: DurableStore,
    call: router_contract.RouterToolCall,
) -> str:
    """Precise, identity-labeled handoff text for send_to_agent."""
    arguments = call.arguments
    project = store.resolve_project(str(arguments["project_slug"]))
    destination = (
        project.display_name
        if project is not None
        else str(arguments["project_slug"])
    )
    target = store.resolve_project_agent(str(arguments["project_slug"]))
    metadata_lines = []
    if target is not None:
        metadata_lines.append(
            "⚙️ "
            + provider_defaults.provider_turn_summary(
                target.provider,
                target.provider_config,
                target.project_path,
            )
        )
        context_snapshot = store.agent_context_snapshot(target.agent_id)
        if context_snapshot is not None:
            metadata_lines.append(
                f"📊 Context before this turn: {context_snapshot}"
            )
    metadata = "\n".join(metadata_lines)
    if metadata:
        metadata = f"\n\n{metadata}"
    result = handoff_message(
        destination,
        (
            f"Sending: {arguments['message']}"
            f"{metadata}\n\nWaiting for {destination}…"
        ),
    )
    return result[:3800]


def annotate_discovery_refs(
    result: dict[str, Any],
    existing_refs: dict[str, dict[str, Any]],
    derived_from: str,
    roots: list[Path],
    ref_budget: int,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Attach controller-issued opaque ref IDs to every discovered path.

    Refs are the only trusted provenance for discovered paths: the model may
    reference a filesystem location in a mutation proposal only through a
    ref the controller issued here (or through text the user wrote). A ref
    is issued only for paths inside the authorized discovery roots — a
    containing Git root that lies above the roots stays visible as plain
    text but can never be used for enrollment — and issuance stops at the
    per-turn ref budget.
    """
    path_to_ref = {
        str(info.get("path")): ref for ref, info in existing_refs.items()
    }
    issued: dict[str, dict[str, Any]] = {}

    def ref_for(path: str) -> Optional[str]:
        if path in path_to_ref:
            return path_to_ref[path]
        if len(issued) >= ref_budget:
            result["refs_truncated"] = True
            return None
        if not discovery.within_roots(path, roots):
            return None
        existing_ids = set(existing_refs) | set(issued)
        ref = f"loc_{uuid.uuid4().hex[:8]}"
        while ref in existing_ids:
            ref = f"loc_{uuid.uuid4().hex[:8]}"
        path_to_ref[path] = ref
        issued[ref] = {
            "path": path,
            "source": "read_only_discovery",
            "derived_from": derived_from[:200],
        }
        return ref

    def annotate_entry(entry: dict[str, Any]) -> None:
        path = entry.get("path")
        if isinstance(path, str) and path:
            ref = ref_for(path)
            if ref is not None:
                entry["ref"] = ref
        git_root = entry.get("containing_git_root")
        if isinstance(git_root, str) and git_root:
            git_root_ref = ref_for(git_root)
            if git_root_ref is not None:
                entry["git_root_ref"] = git_root_ref

    candidates = result.get("candidates")
    if isinstance(candidates, list):
        for entry in candidates:
            if isinstance(entry, dict):
                annotate_entry(entry)
    if result.get("is_directory"):
        annotate_entry(result)
        parent = result.get("path")
        names = result.get("subdirectories")
        if isinstance(parent, str) and isinstance(names, list):
            subdirectory_refs = {}
            for name in names:
                ref = ref_for(str(Path(parent) / str(name)))
                if ref is not None:
                    subdirectory_refs[str(name)] = ref
            result["subdirectory_refs"] = subdirectory_refs
    return result, issued


def annotate_enrollment_metadata(
    store: DurableStore,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Add bounded durable project/agent context to discovered paths."""
    projects = store.list_projects()

    def annotate(entry: dict[str, Any]) -> None:
        path = entry.get("path")
        git_root = entry.get("containing_git_root")
        if not isinstance(path, str):
            return
        matches = []
        for project in projects:
            roles = []
            if path == project.project_path:
                roles.append("repository_root")
            if path == project.working_directory:
                roles.append("working_directory")
            if git_root == project.project_path and not roles:
                roles.append("inside_repository")
            if not roles:
                continue
            agent = store.resolve_project_agent(project.slug)
            matches.append(
                {
                    "project_slug": project.slug,
                    "name": project.display_name,
                    "provider": project.provider,
                    "path_roles": roles,
                    "agent_state": (
                        agent.lifecycle_state
                        if agent is not None
                        else "not created"
                    ),
                }
            )
            if len(matches) >= 4:
                break
        if matches:
            entry["enrolled_projects"] = matches

    candidates = result.get("candidates")
    if isinstance(candidates, list):
        for entry in candidates:
            if isinstance(entry, dict):
                annotate(entry)
    if result.get("is_directory"):
        annotate(result)
    return result


def explicit_absolute_path_in_input(reference: str, user_input: str) -> bool:
    """Require an exact user-authored absolute path, allowing prose punctuation."""
    if not reference or len(reference) > 1000:
        return False
    candidate = Path(reference).expanduser()
    if not candidate.is_absolute():
        return False
    # Path characters on either side indicate a prefix/subpath match. Safe
    # prose delimiters such as quotes, backticks, parentheses, commas, and a
    # sentence-ending period remain accepted.
    start = 0
    invalid_neighbor = set("_/~+@-")
    while True:
        index = user_input.find(reference, start)
        if index < 0:
            return False
        before = user_input[index - 1] if index else ""
        end = index + len(reference)
        after = user_input[end] if end < len(user_input) else ""
        before_ok = (
            not before
            or before.isspace()
            or (
                not before.isalnum()
                and before not in (invalid_neighbor | {"."})
            )
        )
        after_ok = (
            not after
            or after.isspace()
            or after in "'\"`),;:!?]}“”‘’>"
            or (
                after == "."
                and (
                    end + 1 == len(user_input)
                    or user_input[end + 1].isspace()
                    or user_input[end + 1] in "'\"`)]}“”‘’>"
                )
            )
        )
        if before_ok and after_ok:
            return True
        start = index + 1


def project_inspection_text(
    store: DurableStore,
    project_key: str,
    user_input: Optional[str] = None,
    discovery_refs: Optional[dict[str, dict[str, Any]]] = None,
    roots: Optional[list[Path]] = None,
    deadline: Optional[float] = None,
) -> Optional[str]:
    project = store.resolve_project(project_key)
    if project is not None:
        project_path = project.project_path
        git_repository_root = project.git_repository_root
        display_name = project.display_name
        provider = project.provider
        agent = store.resolve_project_agent(project.slug)
    else:
        ref = (discovery_refs or {}).get(project_key)
        if ref is not None:
            raw_path = str(ref.get("path", ""))
        elif (
            user_input is not None
            and explicit_absolute_path_in_input(project_key, user_input)
        ):
            raw_path = project_key
        else:
            return None
        if roots is None:
            roots = discovery.load_discovery_roots()
        if not discovery.within_roots(raw_path, roots):
            return None
        requested_path = Path(os.path.realpath(Path(raw_path).expanduser()))
        if not requested_path.is_dir():
            return None
        project_path = str(requested_path)
        git_repository_root = discovery.exact_git_root(project_path)
        display_name = (
            requested_path.name.replace("-", " ").replace("_", " ").title()
        )
        provider = "not enrolled"
        agent = None
    if agent is None:
        agent_status = "not enrolled" if project is None else "not created"
        session_status = "not started"
        console_status = "not started"
    else:
        agent_status = agent.lifecycle_state
        session_status = (
            "persisted" if agent.provider_session_id is not None else "not started"
        )
        console = store.resolve_agent_console(agent.agent_id)
        console_status = console.state if console is not None else "not started"

    git_status = "not a Git repository"
    try:
        if git_repository_root is None:
            raise RuntimeError("no Git repository")
        remaining = (
            5.0
            if deadline is None
            else min(5.0, deadline - time.monotonic())
        )
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git", 0)
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=git_repository_root,
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
        remaining = (
            5.0
            if deadline is None
            else min(5.0, deadline - time.monotonic())
        )
        if remaining <= 0:
            raise subprocess.TimeoutExpired("git", 0)
        changes_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_repository_root,
            capture_output=True,
            text=True,
            timeout=remaining,
            check=False,
        )
        if branch_result.returncode == 0 and changes_result.returncode == 0:
            branch = branch_result.stdout.strip() or "detached HEAD"
            changes = [
                line for line in changes_result.stdout.splitlines() if line.strip()
            ]
            working_tree = (
                "clean"
                if not changes
                else f"{len(changes)} changed path{'s' if len(changes) != 1 else ''}"
            )
            git_status = f"{branch} · {working_tree}"
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        pass

    return (
        f"🔎 {display_name}\n\n"
        f"Provider: {provider}\n"
        f"Agent: {agent_status}\n"
        f"Session: {session_status}\n"
        f"Console: {console_status}\n"
        f"Git: {git_status}"
    )


def project_catalog_text(store: DurableStore) -> str:
    projects = store.list_projects()
    if not projects:
        return "No projects are enrolled yet."
    lines = ["Enrolled projects", ""]
    aliases = store.project_alias_map()
    for project in projects:
        agent = store.resolve_project_agent(project.slug)
        state = agent.lifecycle_state if agent is not None else "not created"
        line = (
            f"{project.slug} — {project.display_name} "
            f"({project.provider}) · {state}"
        )
        project_aliases = aliases.get(project.slug, [])
        if project_aliases:
            line += "\n  Aliases: " + ", ".join(project_aliases)
        lines.append(line)
    return "\n".join(lines)


def alias_appears_in_input(alias: str, user_input: str) -> bool:
    normalized_alias = " ".join(alias.casefold().split())
    normalized_input = " ".join(user_input.casefold().split())
    return re.search(
        rf"(?<![a-z0-9]){re.escape(normalized_alias)}(?![a-z0-9])",
        normalized_input,
    ) is not None


def resolve_path_reference(
    reference: str,
    user_input: str,
    discovery_refs: dict[str, dict[str, Any]],
    label: str,
) -> tuple[str, dict[str, Any]]:
    """Resolve a model-supplied path reference with trusted provenance.

    A reference is accepted only as a controller-issued discovery ref ID or
    as a path the user themselves wrote; a model-asserted path with neither
    provenance fails closed.
    """
    if reference in discovery_refs:
        info = discovery_refs[reference]
        return str(info["path"]), {
            "value": str(info["path"]),
            "source": "read_only_discovery",
            "derived_from": str(info.get("derived_from", "")),
        }
    if explicit_absolute_path_in_input(reference, user_input):
        return reference, {
            "value": reference,
            "source": "user_request",
            "derived_from": reference,
        }
    raise StoreError(
        f"The {label} must be a discovery ref ID or text from the user's "
        "own request."
    )


def project_creation_proposal(
    store: DurableStore,
    user_input: str,
    arguments: dict[str, Any],
    discovery_refs: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[str, Optional[dict[str, Any]]]:
    refs = discovery_refs or {}
    project_reference = str(arguments["project"]).strip()
    workdir_reference = arguments.get("working_directory")
    identity_name = arguments.get("name")
    if isinstance(identity_name, str):
        identity_name = identity_name.strip()
        # The project identity is user-chosen; it must come from the user's
        # own words, not model invention.
        if not alias_appears_in_input(identity_name, user_input):
            raise StoreError(
                "The project name must appear explicitly in the user's "
                "request."
            )
    topic_name = arguments.get("topic_name")
    requested_provider = arguments.get("provider")
    model = arguments.get("model")
    effort = arguments.get("effort")
    for label, value in (("model", model), ("effort", effort)):
        if isinstance(value, str) and not alias_appears_in_input(value, user_input):
            raise StoreError(
                f"The requested {label} must appear explicitly in the user's request."
            )
    enrolled = store.resolve_project(project_reference)
    provenance: list[dict[str, Any]] = []
    if enrolled is not None:
        if workdir_reference is not None:
            raise StoreError(
                f"{enrolled.display_name} is already enrolled; its working "
                "directory cannot be changed through creation."
            )
        if requested_provider is not None and requested_provider != enrolled.provider:
            raise StoreError(
                f"{enrolled.display_name} is already enrolled for "
                f"{enrolled.provider}, not {requested_provider}."
            )
        existing_agent = store.resolve_project_agent(enrolled.slug)
        if existing_agent is not None:
            return (
                control_message(
                    f"{enrolled.display_name} already has a managed project "
                    "agent."
                ),
                None,
            )
        workspace_root = enrolled.project_path
        working_directory = enrolled.working_directory
        git_repository_root = enrolled.git_repository_root
        slug = enrolled.slug
        display_name = enrolled.display_name
        provider = enrolled.provider
        provenance.append(
            {
                "value": workspace_root,
                "source": "enrolled_project",
                "derived_from": enrolled.slug,
            }
        )
    else:
        root_reference, root_provenance = resolve_path_reference(
            project_reference,
            user_input,
            refs,
            "workspace root",
        )
        workdir_text: Optional[str] = None
        if workdir_reference is not None:
            workdir_text, workdir_provenance = resolve_path_reference(
                str(workdir_reference).strip(),
                user_input,
                refs,
                "working directory",
            )
        # Full validation: real paths and containment. Git is optional
        # metadata, detected only when the selected workspace is itself an
        # exact repository root. The same check runs again at confirmation
        # and every agent launch.
        workspace_root, working_directory, _ = (
            discovery.validate_agent_workspace(
                str(Path(root_reference).expanduser()),
                (
                    str(Path(workdir_text).expanduser())
                    if workdir_text is not None
                    else None
                ),
            )
        )
        git_repository_root = discovery.exact_git_root(workspace_root)
        provenance.append(root_provenance)
        if workdir_reference is not None:
            provenance.append(workdir_provenance)
        # The user's stated name is the project identity; the directory name
        # is only a fallback when no name was given.
        if isinstance(identity_name, str) and identity_name:
            source_name = identity_name
            display_name = identity_name
        else:
            source_name = (
                Path(working_directory).name or Path(workspace_root).name
            )
            display_name = (
                source_name.replace("-", " ").replace("_", " ").title()
            )
        slug = re.sub(r"[^a-z0-9]+", "-", source_name.lower()).strip("-")
        if not slug or len(slug) > 48 or slug == "root":
            raise StoreError("A safe project slug could not be derived.")
        collision = store.resolve_project(slug)
        if collision is not None and (
            collision.project_path != workspace_root
            or collision.working_directory != working_directory
        ):
            raise StoreError("Another enrolled project already uses this slug.")
        provider = (
            str(requested_provider)
            if requested_provider in {"codex", "claude"}
            else "codex"
        )
    resolved_topic = (
        str(topic_name).strip()
        if isinstance(topic_name, str) and topic_name.strip()
        else display_name
    )
    provider_config = {
        key: value
        for key, value in (("model", model), ("effort", effort))
        if isinstance(value, str)
    }
    provider_config = validate_provider_config(provider, provider_config)
    plan = {
        "slug": slug,
        "display_name": display_name,
        "provider": provider,
        "project_path": workspace_root,
        "working_directory": working_directory,
        "git_repository_root": git_repository_root,
        "topic_name": resolved_topic,
        "provider_config": provider_config,
        "provenance": provenance,
    }
    model_text, effort_text = provider_defaults.describe_provider_config(
        provider,
        provider_config,
        workspace_root,
    )
    workdir_line = (
        f"Working directory: {working_directory}\n"
        if working_directory != workspace_root
        else ""
    )
    return (
        control_message(
            f"I found the {display_name} workspace at {workspace_root}.\n"
            f"{workdir_line}"
            f"Git: {'repository detected' if git_repository_root else 'not required'}\n"
            f"Provider: {provider}\n"
            f"Model: {model_text}\n"
            f"Effort: {effort_text}\n"
            f"Telegram topic: {resolved_topic}\n\n"
            "I validated the workspace boundary and working directory. Nothing "
            "will be created until you confirm."
        ),
        plan,
    )


def forum_workspace_proposal(
    store: DurableStore,
    chat_id: int,
    user_input: str,
    arguments: dict[str, Any],
    discovery_refs: Optional[dict[str, dict[str, Any]]] = None,
) -> tuple[str, Optional[dict[str, Any]]]:
    """Build a confirmation-gated workspace binding for this private forum."""
    if int(chat_id) >= 0:
        raise StoreError(
            "Forum workspace binding is available only inside an authorized "
            "private forum."
        )
    forum_binding = store.resolve_surface_binding(chat_id, None)
    if (
        forum_binding is None
        or forum_binding.surface_type != "control"
        or forum_binding.target_type != "controller"
        or forum_binding.target_id != "control"
    ):
        raise StoreError("This private forum is not authorized for Control.")
    existing = store.resolve_forum_workspace(chat_id)
    if existing is not None:
        return (
            control_message(
                f"{existing.display_name} is already bound to its workspace."
            ),
            None,
        )

    refs = discovery_refs or {}
    workspace_reference = str(arguments["workspace"]).strip()
    workdir_reference = arguments.get("working_directory")
    requested_provider = arguments.get("provider")
    model = arguments.get("model")
    effort = arguments.get("effort")
    for label, value in (("model", model), ("effort", effort)):
        if isinstance(value, str) and not alias_appears_in_input(value, user_input):
            raise StoreError(
                f"The requested {label} must appear explicitly in the user's request."
            )

    enrolled = store.resolve_project(workspace_reference)
    provenance: list[dict[str, Any]] = []
    if enrolled is not None:
        if workdir_reference is not None:
            raise StoreError(
                f"{enrolled.display_name} is already enrolled; its working "
                "directory cannot be changed while binding this forum."
            )
        if enrolled.provider not in {"codex", "claude"}:
            raise StoreError(
                "That project's provider cannot back a forum workspace."
            )
        workspace_root = enrolled.project_path
        working_directory = enrolled.working_directory
        git_repository_root = enrolled.git_repository_root
        provenance.append(
            {
                "value": workspace_root,
                "source": "enrolled_project",
                "derived_from": enrolled.slug,
            }
        )
    else:
        root_reference, root_provenance = resolve_path_reference(
            workspace_reference,
            user_input,
            refs,
            "forum workspace root",
        )
        workdir_text: Optional[str] = None
        if workdir_reference is not None:
            workdir_text, workdir_provenance = resolve_path_reference(
                str(workdir_reference).strip(),
                user_input,
                refs,
                "forum working directory",
            )
        workspace_root, working_directory, _ = (
            discovery.validate_agent_workspace(
                str(Path(root_reference).expanduser()),
                (
                    str(Path(workdir_text).expanduser())
                    if workdir_text is not None
                    else None
                ),
            )
        )
        git_repository_root = discovery.exact_git_root(workspace_root)
        provenance.append(root_provenance)
        if workdir_reference is not None:
            provenance.append(workdir_provenance)

    provider = str(requested_provider or "codex")
    if provider not in {"codex", "claude"}:
        raise StoreError("A forum workspace requires Codex or Claude.")
    provider_config = validate_provider_config(
        provider,
        {
            key: value
            for key, value in (("model", model), ("effort", effort))
            if isinstance(value, str)
        },
    )
    plan = {
        "chat_id": int(chat_id),
        "forum_binding_id": forum_binding.binding_id,
        "display_name": forum_binding.display_name,
        "project_path": workspace_root,
        "working_directory": working_directory,
        "git_repository_root": git_repository_root,
        "provider": provider,
        "provider_config": provider_config,
        "provenance": provenance,
    }
    workdir_line = (
        f"Working directory: {working_directory}\n"
        if working_directory != workspace_root
        else ""
    )
    model_text, effort_text = provider_defaults.describe_provider_config(
        provider,
        provider_config,
        workspace_root,
    )
    return (
        control_message(
            f"Bind the {forum_binding.display_name} forum to this workspace?\n\n"
            f"Workspace: {workspace_root}\n"
            f"{workdir_line}"
            f"Git: {'repository detected' if git_repository_root else 'not required'}\n"
            f"Provider: {provider}\n"
            f"Model: {model_text}\n"
            f"Effort: {effort_text}\n\n"
            "Every topic in this forum will stay inside this workspace "
            "boundary. Nothing changes until you confirm."
        ),
        plan,
    )


def topic_rename_proposal(
    store: DurableStore,
    chat_id: int,
    user_input: str,
    arguments: dict[str, Any],
) -> tuple[str, Optional[dict[str, Any]]]:
    message_thread_id = int(arguments["message_thread_id"])
    new_name = str(arguments["name"]).strip()
    if not alias_appears_in_input(new_name, user_input):
        raise StoreError(
            "The new topic name must appear explicitly in the user's request."
        )
    binding = store.resolve_surface_binding(chat_id, message_thread_id)
    if binding is None or binding.message_thread_id is None:
        raise StoreError("The selected managed Telegram topic no longer exists.")
    if binding.display_name == new_name:
        return (
            control_message(f"The topic is already named “{new_name}”."),
            None,
        )
    plan = {
        "binding_id": binding.binding_id,
        "chat_id": binding.chat_id,
        "message_thread_id": binding.message_thread_id,
        "old_name": binding.display_name,
        "new_name": new_name,
    }
    return (
        control_message(
            "Rename this Telegram topic?\n\n"
            f"Current name: {binding.display_name}\n"
            f"New name: {new_name}\n"
            f"Topic ID: {binding.message_thread_id}\n\n"
            "Telegram and the durable controller binding will be updated "
            "only after you confirm."
        ),
        plan,
    )


def reply_dispatch_authorized(
    store: DurableStore,
    project,
    user_request_text: str,
) -> bool:
    """Decide whether a reply-context turn may dispatch without confirmation.

    Quoted bot text must never authorize a dispatch by itself, so the
    user-authored reply has to name the destination explicitly by canonical
    slug, display name, or durable alias.
    """
    if project is None:
        return False
    candidates = {
        project.slug,
        project.slug.replace("-", " "),
        project.display_name,
    }
    candidates.update(store.project_alias_map().get(project.slug, []))
    return any(
        alias_appears_in_input(candidate, user_request_text)
        for candidate in candidates
        if candidate
    )


def router_rotation_reason(metrics: dict[str, Any]) -> Optional[str]:
    if int(metrics["input_tokens"]) >= ROUTER_MAX_INPUT_TOKENS:
        return (
            f"input tokens reached {int(metrics['input_tokens']):,} "
            f"(limit {ROUTER_MAX_INPUT_TOKENS:,})"
        )
    if int(metrics["completed_turns"]) >= ROUTER_MAX_COMPLETED_TURNS:
        return (
            f"completed turns reached {int(metrics['completed_turns'])} "
            f"(limit {ROUTER_MAX_COMPLETED_TURNS})"
        )
    return None


class RouterLeaseKeeper:
    """Renew one router lease independently of provider output."""

    def __init__(
        self,
        database_path: Path,
        mailbox_id: int,
        worker_id: str,
        lease_seconds: float,
    ):
        self.database_path = Path(database_path)
        self.mailbox_id = int(mailbox_id)
        self.worker_id = worker_id
        self.lease_seconds = max(0.25, float(lease_seconds))
        self.interval = max(0.05, min(30.0, self.lease_seconds / 3.0))
        self.stop_event = threading.Event()
        self.lost_event = threading.Event()
        self.error: Optional[BaseException] = None
        self.thread = threading.Thread(
            target=self._run,
            name=f"router-lease-{self.mailbox_id}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        try:
            with DurableStore(self.database_path) as keeper_store:
                while not self.stop_event.wait(self.interval):
                    keeper_store.heartbeat_router_mailbox(
                        self.mailbox_id,
                        self.worker_id,
                        lease_seconds=self.lease_seconds,
                    )
        except BaseException as exc:
            self.error = exc
            self.lost_event.set()

    def assert_owned(self) -> None:
        if self.lost_event.is_set():
            detail = str(self.error) if self.error is not None else "unknown error"
            raise LeaseLostError(
                f"Router mailbox lease keeper stopped: {detail}"
            )

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=max(1.0, self.interval * 2.0))
        if self.thread.is_alive():
            self.lost_event.set()
            raise LeaseLostError("Router mailbox lease keeper did not stop.")


def process_router_mailbox_job(
    store: DurableStore,
    job: RouterMailboxJob,
    worker_id: str,
    lease_seconds: float = 10 * 60,
) -> None:
    effective_lease_seconds = max(0.25, float(lease_seconds))
    lease_keeper = RouterLeaseKeeper(
        store.path,
        job.mailbox_id,
        worker_id,
        effective_lease_seconds,
    )
    store.heartbeat_router_mailbox(
        job.mailbox_id,
        worker_id,
        lease_seconds=effective_lease_seconds,
    )
    lease_keeper.start()
    try:
        mailbox_session_id = job.provider_session_id
        if mailbox_session_id is not None and job.attempts == 1:
            metrics = store.router_session_metrics(mailbox_session_id)
            rotation_reason = router_rotation_reason(metrics)
            if rotation_reason is not None:
                old_session_id = store.rotate_main_router_session(
                    job.mailbox_id,
                    worker_id,
                    rotation_reason,
                )
                mailbox_session_id = None
                log_event(
                    "router_session_rotated",
                    mailbox_id=job.mailbox_id,
                    old_provider_session_id=old_session_id,
                    reason=rotation_reason,
                )
        store.enqueue_router_voice_status(
            job.source_inbox_job_id,
            "working",
            job.input_text,
        )
        main_agent = store.resolve_main_agent()
        if main_agent is None:
            raise StoreError("Main router agent no longer exists.")
        runtime_agent = replace(
            main_agent,
            project_path=str(SCRIPT_PATH.parent),
            provider_config={"sandbox": "read-only"},
        )
        projects = store.list_projects()
        topics = store.list_topic_surfaces(job.chat_id)
        forum_workspace = (
            store.resolve_forum_workspace(job.chat_id)
            if job.chat_id < 0
            else None
        )
        forum_binding = (
            store.resolve_surface_binding(job.chat_id, None)
            if job.chat_id < 0
            else None
        )
        current_surface = {
            "kind": (
                "private_forum_topic"
                if job.chat_id < 0 and forum_binding is not None
                else "private_control"
            ),
            "message_thread_id": job.message_thread_id,
            "forum_authorized": bool(
                job.chat_id < 0
                and forum_binding is not None
                and forum_binding.target_type == "controller"
                and forum_binding.target_id == "control"
            ),
            "forum_name": (
                forum_binding.display_name
                if forum_binding is not None
                else None
            ),
            "workspace_bound": forum_workspace is not None,
            "workspace_name": (
                forum_workspace.display_name
                if forum_workspace is not None
                else None
            ),
            "provider": (
                forum_workspace.provider
                if forum_workspace is not None
                else None
            ),
        }
        prompt = router_contract.build_main_agent_prompt(
            job.input_text,
            projects,
            store.list_project_agent_states(),
            store.project_alias_map(),
            topics,
            current_surface,
        )
        # Bounded multi-step loop: read-only discovery calls repeat until the
        # model returns one terminal tool. Completed steps are persisted, so
        # a crash-recovery retry resumes from recorded history.
        discovery_state = store.load_router_discovery(job.mailbox_id)
        if job.attempts > 1 and discovery_state["steps"]:
            prompt = (
                prompt
                + "\n\n"
                + router_contract.build_discovery_recap(
                    discovery_state["steps"]
                )
            )
        adapter = provider_adapters.adapter_for(runtime_agent)
        allowed_slugs = {project.slug for project in projects}
        alias_resolution = store.project_alias_resolution()
        allowed_topics = {
            int(topic.message_thread_id)
            for topic in topics
            if topic.message_thread_id is not None
        }
        loop_started = time.monotonic()
        loop_deadline = loop_started + ROUTER_MAX_LOOP_SECONDS
        next_input = prompt
        discovery_roots: Optional[list[Path]] = None
        forced_terminal: Optional[str] = None
        aggregated_usage: dict[str, int] = {}

        def merge_usage(turn_usage: dict[str, Any]) -> None:
            for key, value in turn_usage.items():
                if isinstance(value, (int, float)) and not isinstance(
                    value, bool
                ):
                    aggregated_usage[key] = int(
                        aggregated_usage.get(key, 0)
                    ) + int(value)
        # Only the first provider call carries the mailbox session (which the
        # adapters treat as crash-recovery); loop continuations resume the
        # session through the runtime agent so discovery-result messages are
        # delivered verbatim.
        session_argument = mailbox_session_id
        while True:
            remaining = loop_deadline - time.monotonic()
            if remaining < 1.0:
                forced_terminal = (
                    "I ran out of investigation time before finishing. "
                    "Please narrow the request."
                )
                break
            # Each provider turn is bounded by the remaining loop budget so
            # the whole turn stays inside the router lease.
            if hasattr(adapter, "timeout_seconds"):
                adapter.timeout_seconds = max(1, int(remaining))

            def provider_heartbeat() -> None:
                lease_keeper.assert_owned()
                store.heartbeat_router_mailbox(
                    job.mailbox_id,
                    worker_id,
                    lease_seconds=effective_lease_seconds,
                )

            provider_heartbeat()
            result = adapter.run_turn(
                runtime_agent,
                next_input,
                session_argument,
                on_session=lambda session_id: (
                    store.attach_router_mailbox_session(
                        job.mailbox_id,
                        worker_id,
                        session_id,
                    )
                ),
                heartbeat=provider_heartbeat,
            )
            lease_keeper.assert_owned()
            if time.monotonic() > loop_deadline:
                forced_terminal = (
                    "I ran out of investigation time before finishing. "
                    "Please narrow the request."
                )
                break
            merge_usage(result.usage)
            mailbox_session_id = result.provider_session_id
            runtime_agent = replace(
                runtime_agent,
                provider_session_id=result.provider_session_id,
            )
            session_argument = None
            call = router_contract.parse_router_tool_call(
                result.final_text,
                allowed_slugs,
                alias_resolution,
                allowed_topics,
            )
            if call.tool not in router_contract.DISCOVERY_TOOL_NAMES:
                break
            steps_used = len(discovery_state["steps"])
            if (
                steps_used >= ROUTER_MAX_DISCOVERY_STEPS
                or time.monotonic() >= loop_deadline
                or len(discovery_state["refs"]) >= ROUTER_MAX_DISCOVERY_REFS
            ):
                forced_terminal = (
                    "I hit the investigation limit before fully resolving "
                    "this. Please narrow the request — for example name the "
                    "directory or repository more precisely."
                )
                break
            arguments = dict(call.arguments)
            if (
                call.tool == "inspect_directory"
                and arguments.get("path") in discovery_state["refs"]
            ):
                arguments["path"] = str(
                    discovery_state["refs"][arguments["path"]]["path"]
                )
            # Provenance always traces to the user's own bounded request,
            # never to a model-supplied query or an absolute path.
            derived_from = router_contract.extract_user_request(
                job.input_text
            )[:200]
            try:
                if discovery_roots is None:
                    discovery_roots = discovery.load_discovery_roots()
                step_result = discovery.execute_discovery_tool(
                    call.tool,
                    arguments,
                    discovery_roots,
                    deadline=loop_deadline,
                )
            except StoreError as exc:
                step_result = {"error": str(exc)}
            step_result = annotate_enrollment_metadata(store, step_result)
            step_result, issued_refs = annotate_discovery_refs(
                step_result,
                discovery_state["refs"],
                derived_from,
                discovery_roots or [],
                max(
                    0,
                    ROUTER_MAX_DISCOVERY_REFS
                    - len(discovery_state["refs"]),
                ),
            )
            discovery_state = store.append_router_discovery_step(
                job.mailbox_id,
                worker_id,
                call.tool,
                call.arguments,
                step_result,
                issued_refs,
            )
            log_event(
                "router_discovery_step",
                mailbox_id=job.mailbox_id,
                tool=call.tool,
                steps=len(discovery_state["steps"]),
                refs=len(discovery_state["refs"]),
            )
            next_input = router_contract.build_discovery_result_message(
                call.tool,
                call.arguments,
                step_result,
            )
        if forced_terminal is not None:
            call = router_contract.RouterToolCall(
                tool="respond",
                arguments={"message": forced_terminal},
                requires_confirmation=False,
            )
        # Explicit-mention validations must only consider the user-authored
        # part of the input, never quoted reply context.
        user_request_text = router_contract.extract_user_request(job.input_text)
        if call.tool == "send_to_agent" and router_contract.has_reply_context(
            job.input_text
        ):
            guarded_project = store.resolve_project(
                str(call.arguments["project_slug"])
            )
            if not reply_dispatch_authorized(
                store,
                guarded_project,
                user_request_text,
            ):
                destination = (
                    guarded_project.display_name
                    if guarded_project is not None
                    else str(call.arguments["project_slug"])
                )
                call = router_contract.RouterToolCall(
                    tool="ask_user",
                    arguments={
                        "question": (
                            f"Send this follow-up to {destination}?"
                        ),
                        "options": ["Yes, send it", "No, cancel"],
                    },
                    requires_confirmation=False,
                )
                log_event(
                    "router_reply_dispatch_guarded",
                    mailbox_id=job.mailbox_id,
                    project_slug=str(
                        guarded_project.slug
                        if guarded_project is not None
                        else "unknown"
                    ),
                )
        dispatch_agent_id = None
        dispatch_message = None
        clarification_options = None
        project_creation_plan = None
        forum_workspace_plan = None
        topic_rename_plan = None
        agent_config_plan = None
        if call.tool == "send_to_agent":
            target = store.resolve_project_agent(
                str(call.arguments["project_slug"])
            )
            if target is None:
                raise StoreError(
                    "The selected project has no active managed agent."
                )
            dispatch_agent_id = target.agent_id
            dispatch_message = str(call.arguments["message"])
            response_text = dispatch_preview_text(store, call)
        elif call.tool == "inspect_project":
            inspection = project_inspection_text(
                store,
                str(call.arguments["project"]),
                user_request_text,
                discovery_state["refs"],
                discovery_roots,
                loop_deadline,
            )
            response_text = (
                control_message(inspection)
                if inspection is not None
                else control_message(
                    "I could not validate that project or path read-only. "
                    "Nothing was inspected — name an enrolled project, or "
                    "describe the directory so I can locate it."
                )
            )
        elif call.tool == "list_projects":
            response_text = control_message(project_catalog_text(store))
        elif call.tool == "respond":
            response_text = control_message(str(call.arguments["message"]))
        elif call.tool == "ask_user":
            response_text = control_message(str(call.arguments["question"]))
            clarification_options = list(call.arguments["options"]) or None
        elif call.tool == "create_project_agent":
            response_text, project_creation_plan = project_creation_proposal(
                store,
                user_request_text,
                call.arguments,
                discovery_state["refs"],
            )
        elif call.tool == "bind_forum_workspace":
            response_text, forum_workspace_plan = forum_workspace_proposal(
                store,
                job.chat_id,
                user_request_text,
                call.arguments,
                discovery_state["refs"],
            )
        elif call.tool == "rename_topic":
            response_text, topic_rename_plan = topic_rename_proposal(
                store,
                job.chat_id,
                user_request_text,
                call.arguments,
            )
        elif call.tool == "configure_agent":
            project = store.resolve_project(
                str(call.arguments["project_slug"])
            )
            if project is None:
                raise StoreError("The selected project is not enrolled.")
            target = store.resolve_project_agent(project.slug)
            if target is None:
                raise StoreError(
                    "The selected project has no active managed agent."
                )
            updates = {
                key: call.arguments[key]
                for key in ("model", "effort")
                if key in call.arguments
            }
            for label, value in updates.items():
                if (
                    isinstance(value, str)
                    and not alias_appears_in_input(value, user_request_text)
                ):
                    raise StoreError(
                        f"The requested {label} must appear explicitly in "
                        "the user's request."
                    )
            agent_config_plan = {
                "project_slug": project.slug,
                "updates": updates,
            }
            planned_config = dict(target.provider_config)
            for key, value in updates.items():
                if value is None:
                    planned_config.pop(key, None)
                else:
                    planned_config[key] = value
            planned_model, planned_effort = (
                provider_defaults.describe_provider_config(
                    target.provider,
                    planned_config,
                    target.project_path,
                )
            )
            planned_labels = {
                "model": planned_model,
                "effort": planned_effort,
            }
            update_lines = "\n".join(
                f"{key.title()}: {planned_labels[key]}"
                for key in updates
            )
            response_text = control_message(
                f"Change {project.display_name}'s configuration?\n\n"
                f"{update_lines}\n\n"
                "Nothing changes until you confirm."
            )
        elif call.tool == "set_project_alias":
            alias = str(call.arguments["alias"])
            if not alias_appears_in_input(alias, user_request_text):
                raise StoreError(
                    "The project alias must appear explicitly in the user's request."
                )
            project = store.resolve_project(str(call.arguments["project_slug"]))
            if project is None:
                raise StoreError("The selected project is not enrolled.")
            created = store.add_project_alias(project.slug, alias)
            response_text = control_message(
                f"{project.display_name} can now be called “{alias}”."
                if created
                else f"“{alias}” is already an alias for {project.display_name}."
            )
        elif call.tool == "remove_project_alias":
            alias = str(call.arguments["alias"])
            if not alias_appears_in_input(alias, user_request_text):
                raise StoreError(
                    "The project alias must appear explicitly in the user's request."
                )
            project = store.remove_project_alias(alias)
            response_text = control_message(
                f"Removed the alias “{alias}” from {project.display_name}."
                if project is not None
                else f"“{alias}” is not an active project alias."
            )
        else:
            raise StoreError(
                f"Terminal router tool is not executable: {call.tool}"
            )
        store.complete_router_mailbox(
            job.mailbox_id,
            worker_id,
            result.provider_session_id,
            result.final_text,
            call.tool,
            call.arguments,
            response_text,
            aggregated_usage or result.usage,
            dispatch_agent_id=dispatch_agent_id,
            dispatch_message=dispatch_message,
            clarification_options=clarification_options,
            project_creation_plan=project_creation_plan,
            forum_workspace_plan=forum_workspace_plan,
            topic_rename_plan=topic_rename_plan,
            agent_config_plan=agent_config_plan,
        )
        log_event(
            "router_turn_succeeded",
            mailbox_id=job.mailbox_id,
            attempts=job.attempts,
            tool=call.tool,
            provider_session_id=result.provider_session_id,
        )
    except LeaseLostError as exc:
        log_event(
            "router_turn_abandoned",
            mailbox_id=job.mailbox_id,
            attempts=job.attempts,
            error=str(exc),
        )
    except (provider_adapters.ProviderAdapterError, OSError, StoreError) as exc:
        state = store.fail_router_mailbox(
            job.mailbox_id,
            worker_id,
            str(exc),
        )
        log_event(
            "router_turn_failed",
            mailbox_id=job.mailbox_id,
            attempts=job.attempts,
            state=state,
            error=str(exc),
        )
    finally:
        try:
            lease_keeper.stop()
        except LeaseLostError as exc:
            log_event(
                "router_lease_keeper_stop_failed",
                mailbox_id=job.mailbox_id,
                error=str(exc),
            )


def work_router_command(args: argparse.Namespace) -> None:
    worker_id = process_name("router")
    with open_store(args.db) as store:
        while True:
            job = store.claim_router_mailbox(
                worker_id,
                lease_seconds=args.lease_seconds,
            )
            if job is None:
                if args.once:
                    return
                time.sleep(args.idle_sleep)
                continue
            process_router_mailbox_job(
                store,
                job,
                worker_id,
                lease_seconds=args.lease_seconds,
            )
            if args.once:
                return


@contextmanager
def outbox_delivery_lock(database_path: Path):
    """Serialize the Telegram delivery critical section across processes.

    Every sender for one controller database must hold this exclusive kernel
    advisory lock from the pre-delivery lease revalidation through the
    Telegram API call and the durable completion or failure record. A paused
    sender keeps the lock, so no other sender can deliver a newer edit ahead
    of an in-flight older one; a crashed sender's lock is released by the
    kernel and normal idempotent retry semantics take over.

    The lock file is derived only from the canonically resolved controller
    database path, so every path spelling of the same database maps to one
    lock. The guarantee is single-host (all sender processes for one database
    run on this Mac), and the lock is intentionally non-reentrant: it is
    acquired exactly once per delivery and never nested.

    Yields the locked descriptor so the delivery path can pass it to the
    Telegram API helper subprocess, which inherits the same open file
    description: BSD flock then stays held until the helper itself exits,
    even if this sender process is SIGKILLed mid-call, so a replacement
    sender cannot deliver a newer edit while an old request is still in
    flight anywhere.
    """
    lock_path = Path(str(Path(database_path).resolve()) + ".send-lock")
    descriptor = os.open(
        str(lock_path),
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
    )
    if descriptor < 3:
        # A standard-fd slot would be overwritten by the helper's stdio
        # redirection, silently defeating pass_fds inheritance; move the
        # same open file description to a safe number.
        try:
            duplicated = fcntl.fcntl(descriptor, fcntl.F_DUPFD, 3)
        except OSError:
            os.close(descriptor)
            raise
        os.close(descriptor)
        descriptor = duplicated
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    try:
        yield descriptor
    finally:
        # Release by closing, never by explicit LOCK_UN: an unlock would
        # strip the lock from a still-running helper that shares this open
        # file description (for example after a failed kill/reap), whereas
        # closing only drops this process's reference — the kernel releases
        # the lock exactly when the last holder exits.
        os.close(descriptor)


def send_outbox_message(
    store: DurableStore,
    token: str,
    message: OutboxMessage,
    worker_id: str,
) -> None:
    with outbox_delivery_lock(store.path) as delivery_lock_fd:
        # Revalidate ownership after possibly waiting for the lock — another
        # sender may have recovered this lease and already delivered — and
        # atomically renew the lease so it comfortably outlives the Telegram
        # call's hard whole-operation deadline plus the durable outcome
        # record.
        if not store.revalidate_outbox_lease(message.message_id, worker_id):
            log_event(
                "outbox_lease_lost",
                message_id=message.message_id,
                operation_id=message.operation_id,
                attempts=message.attempts,
            )
            return
        if (
            store.router_final_edit_superseded(message.operation_id)
            or (
                message.method == "editMessageText"
                and store.agent_status_edit_superseded(message.operation_id)
            )
        ):
            # A newer agent-outcome edit for the same routing receipt exists,
            # so delivering this stale preview edit would overwrite the final
            # answer.
            try:
                store.complete_outbox(
                    message.message_id,
                    worker_id,
                    {"skipped": "superseded"},
                )
            except LeaseLostError:
                log_event(
                    "outbox_lease_lost",
                    message_id=message.message_id,
                    operation_id=message.operation_id,
                    attempts=message.attempts,
                )
                return
            log_event(
                "outbox_superseded",
                message_id=message.message_id,
                operation_id=message.operation_id,
                attempts=message.attempts,
            )
            return
        try:
            result = bridge.api_call(
                token,
                message.method,
                delivery_lock_fd=delivery_lock_fd,
                **message.params,
            )
        except bridge.BridgeError as exc:
            error = str(exc)
            if (
                message.method == "deleteForumTopic"
                and telegram_reports_missing_topic(error)
            ):
                # Topic deletion is convergent: a lost success response
                # followed by "thread not found" means the requested end
                # state already holds.
                try:
                    store.complete_outbox(
                        message.message_id,
                        worker_id,
                        {"deleted": "already_missing"},
                    )
                except LeaseLostError:
                    log_event(
                        "outbox_lease_lost",
                        message_id=message.message_id,
                        operation_id=message.operation_id,
                        attempts=message.attempts,
                    )
                    return
                log_event(
                    "outbox_sent",
                    message_id=message.message_id,
                    operation_id=message.operation_id,
                    attempts=message.attempts,
                    already_missing=True,
                )
                return
            if (
                message.method == "editMessageText"
                and "message is not modified" in error.lower()
            ):
                # Telegram already shows exactly this content, typically
                # because a previous attempt was applied but its
                # acknowledgment was lost. Completing normally keeps retries
                # convergent and lets durable post-acknowledgment effects
                # (like route retargeting) run.
                try:
                    store.complete_outbox(
                        message.message_id,
                        worker_id,
                        {"edited": "not_modified"},
                    )
                except LeaseLostError:
                    log_event(
                        "outbox_lease_lost",
                        message_id=message.message_id,
                        operation_id=message.operation_id,
                        attempts=message.attempts,
                    )
                    return
                log_event(
                    "outbox_sent",
                    message_id=message.message_id,
                    operation_id=message.operation_id,
                    attempts=message.attempts,
                    not_modified=True,
                )
                return
            handle_outbox_send_failure(store, message, worker_id, error)
            return
        try:
            store.complete_outbox(message.message_id, worker_id, result)
        except LeaseLostError:
            # The lease expired during the Telegram call and another sender
            # reclaimed the row; it will revalidate under this same lock
            # before acting, so delivery order is preserved either way.
            log_event(
                "outbox_lease_lost",
                message_id=message.message_id,
                operation_id=message.operation_id,
                attempts=message.attempts,
                delivered=True,
            )
            return
        voice_file_path = message.params.get("__voice_file_path")
        if voice_file_path is not None:
            voice_responses.remove_voice_file(str(voice_file_path))
        log_event(
            "outbox_sent",
            message_id=message.message_id,
            operation_id=message.operation_id,
            attempts=message.attempts,
        )


def handle_outbox_send_failure(
    store: DurableStore,
    message: OutboxMessage,
    worker_id: str,
    error: str,
) -> None:
    permanent_card_edit_failure = (
        message.method == "editMessageText"
        and message.card is not None
        and message.card.get("mode") in {"edit", "final_edit"}
        and any(
            marker in error.lower()
            for marker in (
                "message to edit not found",
                "message can't be edited",
                "message_id_invalid",
            )
        )
    )
    try:
        state = store.fail_outbox(
            message.message_id,
            worker_id,
            error,
            max_attempts=message.attempts if permanent_card_edit_failure else 8,
        )
    except LeaseLostError:
        log_event(
            "outbox_lease_lost",
            message_id=message.message_id,
            operation_id=message.operation_id,
            attempts=message.attempts,
        )
        return
    if permanent_card_edit_failure and state == "dead":
        if message.card.get("kind") == "agent_turn":
            store.enqueue_agent_response_fallback(
                int(message.card["mailbox_id"])
            )
        elif message.card.get("kind") == "router_turn":
            store.enqueue_router_response_fallback(
                int(message.card["mailbox_id"])
            )
        elif message.card.get("kind") == "agent_control":
            # The original steering receipt remains truthful ("Steering…");
            # the durable control outcome is still retained for inspection.
            pass
        else:
            store.mark_surface_card_stale(int(message.card["card_id"]))
    if state == "dead" and message.params.get("__voice_file_path") is not None:
        voice_responses.remove_voice_file(
            str(message.params["__voice_file_path"])
        )
    log_event(
        "outbox_failed",
        message_id=message.message_id,
        operation_id=message.operation_id,
        attempts=message.attempts,
        state=state,
        error=error,
    )


def send_outbox_command(args: argparse.Namespace) -> None:
    token = bridge.read_token()
    worker_id = process_name("outbox")
    with open_store(args.db) as store:
        while True:
            message = store.claim_outbox(
                worker_id,
                lease_seconds=args.lease_seconds,
            )
            if message is None:
                if args.once:
                    return
                time.sleep(args.idle_sleep)
                continue
            send_outbox_message(store, token, message, worker_id)
            if args.once:
                return


def telegram_reports_missing_topic(error: str) -> bool:
    """Recognize only Telegram's definitive missing-topic responses."""
    normalized = " ".join(str(error).casefold().split())
    return any(marker in normalized for marker in MISSING_TOPIC_ERROR_MARKERS)


def probe_due_topics_once(
    store: DurableStore,
    token: str,
    *,
    interval_seconds: float = TOPIC_PROBE_INTERVAL_SECONDS,
    batch_size: int = TOPIC_PROBE_BATCH_SIZE,
    now: Optional[float] = None,
) -> dict[str, int]:
    """Reconcile active local routes against Telegram topic existence.

    Telegram's Bot API has no read-only topic lookup, and no-op topic edits
    return success even for nonexistent IDs. A silent invisible message is
    therefore sent to each due topic and deleted immediately. Only an
    explicit missing-thread response retires local state. Permission, network,
    closed-topic, malformed-result, and all other failures remain
    inconclusive.
    """
    timestamp = time.time() if now is None else float(now)
    counts = {"probed": 0, "alive": 0, "retired": 0, "deferred": 0}
    candidates = store.list_due_topic_probes(
        now=timestamp,
        interval_seconds=interval_seconds,
        limit=batch_size,
    )
    for candidate in candidates:
        counts["probed"] += 1
        try:
            result = bridge.api_call(
                token,
                "sendMessage",
                chat_id=candidate.chat_id,
                message_thread_id=candidate.message_thread_id,
                text=TOPIC_PROBE_TEXT,
                disable_notification=True,
                protect_content=True,
            )
        except bridge.BridgeError as exc:
            error = str(exc)
            if telegram_reports_missing_topic(error):
                retired = store.retire_missing_topic(
                    candidate.binding_id,
                    reason=error,
                    now=timestamp,
                )
                counts["retired" if retired else "deferred"] += 1
                log_event(
                    "topic_probe_missing",
                    binding_id=candidate.binding_id,
                    chat_id=candidate.chat_id,
                    message_thread_id=candidate.message_thread_id,
                    retired=retired,
                )
            else:
                store.record_topic_probe(
                    candidate.binding_id,
                    error=error,
                    now=timestamp,
                )
                counts["deferred"] += 1
                log_event(
                    "topic_probe_result",
                    binding_id=candidate.binding_id,
                    chat_id=candidate.chat_id,
                    message_thread_id=candidate.message_thread_id,
                    result="inconclusive",
                )
        else:
            probe_message_id = (
                int(result["message_id"])
                if isinstance(result, dict)
                and isinstance(result.get("message_id"), int)
                else None
            )
            if probe_message_id is None:
                error = "Telegram returned no message ID for the topic probe."
                store.record_topic_probe(
                    candidate.binding_id,
                    error=error,
                    now=timestamp,
                )
                counts["deferred"] += 1
                log_event(
                    "topic_probe_result",
                    binding_id=candidate.binding_id,
                    chat_id=candidate.chat_id,
                    message_thread_id=candidate.message_thread_id,
                    result="inconclusive",
                )
                continue
            cleanup_error = None
            try:
                bridge.api_call(
                    token,
                    "deleteMessage",
                    chat_id=candidate.chat_id,
                    message_id=probe_message_id,
                )
            except bridge.BridgeError as exc:
                # The successful send already proved that the topic exists.
                # Record a possible orphaned invisible probe for diagnostics.
                cleanup_error = (
                    f"Topic exists, but its probe message could not be "
                    f"deleted: {exc}"
                )
            store.record_topic_probe(
                candidate.binding_id,
                error=cleanup_error,
                now=timestamp,
            )
            counts["alive"] += 1
            log_event(
                "topic_probe_result",
                binding_id=candidate.binding_id,
                chat_id=candidate.chat_id,
                message_thread_id=candidate.message_thread_id,
                result="alive",
                probe_deleted=cleanup_error is None,
            )
    return counts


def maintain_topics_command(args: argparse.Namespace) -> None:
    """Periodically retire routes for topics deleted directly in Telegram."""
    token = bridge.read_token()
    with open_store(args.db) as store:
        while True:
            counts = probe_due_topics_once(
                store,
                token,
                interval_seconds=args.interval_seconds,
                batch_size=args.batch_size,
            )
            if counts["probed"]:
                log_event("topic_maintenance_cycle", **counts)
            if args.once:
                return
            time.sleep(args.idle_sleep)


def supervisor_commands(
    database_path: Path,
    agent_workers: int = DEFAULT_AGENT_WORKERS,
    control_agent_enabled: bool = False,
) -> list[list[str]]:
    count = int(agent_workers)
    if count < 1 or count > MAX_AGENT_WORKERS:
        raise StoreError(
            f"Agent worker count must be between 1 and {MAX_AGENT_WORKERS}."
        )
    base = [sys.executable, str(SCRIPT_PATH), "--db", str(database_path)]
    commands = [
        [*base, "collect"],
        [*base, "work"],
    ]
    if control_agent_enabled:
        commands.append([*base, "work-router"])
    commands.extend([*base, "work-agents"] for _ in range(count))
    commands.append([*base, "maintain-workers"])
    commands.append([*base, "maintain-topics"])
    commands.append([*base, "send-outbox"])
    return commands


def cleanup_reload_jobs() -> list[str]:
    """Remove stale one-shot reload jobs before controller workers start."""
    result = bridge.launchctl("list", check=False)
    if result.returncode != 0:
        return []
    labels = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields:
            continue
        label = fields[-1]
        if label == RELOAD_JOB_PREFIX or label.startswith(
            f"{RELOAD_JOB_PREFIX}."
        ):
            labels.append(label)
    removed = []
    for label in sorted(set(labels)):
        removal = bridge.launchctl("remove", label, check=False)
        if removal.returncode == 0:
            removed.append(label)
    return removed


def run_command(args: argparse.Namespace) -> None:
    """Run the independently restartable controller loops under a supervisor."""
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def stop_on_sigterm(_: int, __: Any) -> None:
        # launchd uses SIGTERM for controlled restarts. SystemExit still runs
        # the cleanup block but does not write a false alarm to the error log.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_on_sigterm)
    removed_reload_jobs = cleanup_reload_jobs()
    if removed_reload_jobs:
        log_event(
            "stale_reload_jobs_removed",
            labels=removed_reload_jobs,
        )
    config = bridge.load_config()
    try:
        control_enabled = app_config.control_agent_enabled(config)
    except app_config.ConfigError as exc:
        raise StoreError(str(exc)) from None
    commands = supervisor_commands(
        args.db,
        args.agent_workers,
        control_agent_enabled=control_enabled,
    )
    processes: list[subprocess.Popen[Any]] = []
    try:
        for command in commands:
            processes.append(subprocess.Popen(command))
        log_event(
            "supervisor_started",
            child_pids=[process.pid for process in processes],
            agent_workers=int(args.agent_workers),
            control_agent_enabled=control_enabled,
        )
        next_restart_check = time.time()
        while True:
            for process in processes:
                return_code = process.poll()
                if return_code is not None:
                    raise StoreError(
                        f"Controller child {process.pid} exited with status {return_code}."
                    )
            if time.time() >= next_restart_check:
                next_restart_check = time.time() + 5
                # A queued restart is applied by exiting: launchd's KeepAlive
                # starts a fresh supervisor, which is a full reload onto current
                # code. The claim only succeeds while nothing is leased, so this
                # never interrupts a turn — including the turn that asked.
                with open_store(args.db) as store:
                    claimed = store.claim_idle_restart()
                if claimed is not None:
                    log_event(
                        "controller_restart_applied",
                        reason=str(claimed.get("reason", "")),
                        requested_at=claimed.get("requested_at"),
                    )
                    raise SystemExit(0)
            time.sleep(1)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        signal.signal(signal.SIGTERM, previous_sigterm)


def launch_agent_plist(database_path: Path = DATABASE_PATH) -> dict[str, Any]:
    return {
        "Label": bridge.LAUNCH_AGENT_LABEL,
        "ProgramArguments": [
            sys.executable,
            str(SCRIPT_PATH),
            "--db",
            str(database_path),
            "run",
        ],
        "WorkingDirectory": str(SCRIPT_PATH.parent),
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 5,
        "ProcessType": "Background",
        "StandardOutPath": str(bridge.LOG_DIR / "telegram-control.log"),
        "StandardErrorPath": str(bridge.LOG_DIR / "telegram-control.error.log"),
    }


def write_plist(path: Path, value: dict[str, Any]) -> None:
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("wb") as plist_file:
        plistlib.dump(value, plist_file)
    bridge.ensure_private_file(temporary_path)
    temporary_path.replace(path)
    bridge.ensure_private_file(path)


def configured_launch_agent_mode() -> str:
    try:
        with bridge.PLIST_PATH.open("rb") as plist_file:
            plist = plistlib.load(plist_file)
    except (FileNotFoundError, OSError, ValueError):
        return "missing"
    arguments = [str(value) for value in plist.get("ProgramArguments", [])]
    if str(SCRIPT_PATH) in arguments and "run" in arguments:
        return "durable"
    if str(bridge.SCRIPT_PATH) in arguments and "listen" in arguments:
        return "stage0"
    return "unknown"


def _resolve_skill_placeholders(destination: Path, repository_root: Path) -> None:
    """Point an installed skill's commands at this checkout."""
    root = str(repository_root.resolve())
    for path in sorted(destination.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        if SKILL_ROOT_PLACEHOLDER not in text:
            continue
        path.write_text(text.replace(SKILL_ROOT_PLACEHOLDER, root))


def install_managed_skills(
    source_directory: Path = SKILLS_SOURCE_DIR,
    shared_directory: Path = SHARED_SKILLS_DIR,
    claude_directory: Path = CLAUDE_SKILLS_DIR,
) -> list[str]:
    """Install repo-owned shared skills and expose them to Claude Code."""
    shared_directory.mkdir(parents=True, exist_ok=True)
    claude_directory.mkdir(parents=True, exist_ok=True)
    installed = []
    for name in MANAGED_SHARED_SKILLS:
        source = source_directory / name
        if not (source / "SKILL.md").is_file():
            raise StoreError(f"Managed skill source is incomplete: {source}")
        destination = shared_directory / name
        if destination.is_symlink():
            raise StoreError(
                f"Shared skill destination must be a real directory: {destination}"
            )
        shutil.copytree(source, destination, dirs_exist_ok=True)
        _resolve_skill_placeholders(destination, source_directory.parent)

        claude_link = claude_directory / name
        relative_target = Path(
            os.path.relpath(destination, start=claude_directory)
        )
        if claude_link.is_symlink():
            if Path(os.readlink(claude_link)) != relative_target:
                raise StoreError(
                    f"Claude skill link points somewhere else: {claude_link}"
                )
        elif claude_link.exists():
            raise StoreError(
                f"Claude skill path already exists and was not replaced: {claude_link}"
            )
        else:
            claude_link.symlink_to(relative_target, target_is_directory=True)
        installed.append(name)
    return installed


def _worker_origin_context(store) -> tuple[int, Optional[str]]:
    """Find the chat and agent that own the managed turn starting a worker."""
    origin_agent_id = os.environ.get("TELEGRAM_CONTROL_AGENT_ID")
    if not origin_agent_id:
        return int(bridge.load_config()["chat_id"]), None

    agent = store.resolve_agent(origin_agent_id)
    if agent is None or agent.surface_binding_id is None:
        raise StoreError(
            "The managed turn's originating Telegram topic is unavailable."
        )
    binding = store.resolve_surface_binding_by_id(agent.surface_binding_id)
    if binding is None:
        raise StoreError(
            "The managed turn's originating Telegram topic is no longer active."
        )
    return binding.chat_id, agent.agent_id


def _ensure_worker_topic(store, name: str, chat_id: int):
    """Create (or find) the worker's report-only topic beside its origin."""
    display_name = detached_worker.topic_name(name)
    existing = store.resolve_named_surface(chat_id, display_name, surface_type="task")
    if existing is not None:
        return existing
    token = bridge.read_token()
    if chat_id > 0:
        bot = bridge.api_call(token, "getMe")
        if not bool(bot.get("has_topics_enabled", False)):
            raise StoreError(
                "Telegram Threaded Mode is disabled for this bot. "
                "Enable it in BotFather first."
            )
    topic = bridge.api_call(
        token,
        "createForumTopic",
        chat_id=chat_id,
        name=display_name,
    )
    try:
        message_thread_id = int(topic["message_thread_id"])
    except (KeyError, TypeError, ValueError):
        raise StoreError("Telegram returned an invalid forum-topic result.") from None
    return store.ensure_surface_binding(
        chat_id=chat_id,
        message_thread_id=message_thread_id,
        surface_type="task",
        display_name=display_name,
        target_type="detached_worker",
        target_id=name,
    )


def worker_start_command(args: argparse.Namespace) -> None:
    project_path = str(Path(args.project_path or os.getcwd()).expanduser().resolve())
    provider_config = {}
    if args.model:
        provider_config["model"] = args.model
    if args.effort:
        provider_config["effort"] = args.effort
    with open_store(args.db) as store:
        chat_id, origin_agent_id = _worker_origin_context(store)
        binding = _ensure_worker_topic(store, args.name, chat_id)
        worker = detached_worker.create_worker(
            store,
            name=args.name,
            binding_id=binding.binding_id,
            project_path=project_path,
            provider=args.provider,
            provider_config=provider_config,
            origin_agent_id=origin_agent_id,
        )
    print(
        json.dumps(
            {
                "name": worker.name,
                "provider": worker.provider,
                "project_path": worker.project_path,
                "tmux_session": worker.tmux_session_name,
                "topic": detached_worker.topic_name(worker.name),
                "message_thread_id": binding.message_thread_id,
                "provider_session_persisted": (
                    worker.provider_session_id is not None
                ),
                "recovery_file": worker.recovery_file_path,
                "attach": f"tmux attach-session -t '={worker.tmux_session_name}'",
            },
            indent=2,
            sort_keys=True,
        )
    )


def worker_brief_command(args: argparse.Namespace) -> None:
    brief = Path(args.file).expanduser().read_text(encoding="utf-8").strip()
    if not brief:
        raise StoreError("Brief file is empty.")
    with open_store(args.db) as store:
        worker = store.resolve_detached_worker(args.name)
        if worker is None:
            raise StoreError("Detached worker was not found.")
        recovery_file = detached_worker.ensure_recovery_file(store, worker.name)
        if worker.recovery_file_path != str(recovery_file):
            worker = store.configure_detached_worker_recovery(
                worker.name,
                provider_session_id=worker.provider_session_id,
                recovery_file_path=str(recovery_file),
                recovery_prompt=detached_worker.DEFAULT_RECOVERY_PROMPT,
            )
    # Delivered verbatim. The worker was told who it is at launch, and a brief
    # is usually a relay of something the owner just said, so anything prepended
    # here is a standing instruction repeated for no one's benefit.
    detached_worker.send_brief(args.name, brief)
    print(f"Brief delivered to {detached_worker.tmux_session_name(args.name)}")


def worker_report_command(args: argparse.Namespace) -> None:
    text = sys.stdin.read()
    with open_store(args.db) as store:
        detached_worker.report(
            store,
            args.name,
            key=args.key,
            text=text,
            mode="text" if args.text else "voice",
        )
    print("Report delivered.")


def worker_status_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        rows = []
        for worker in store.list_detached_workers():
            current = detached_worker.reconcile_worker(store, worker.name) or worker
            rows.append(
                {
                    "name": current.name,
                    "provider": current.provider,
                    "intended": current.intended_state,
                    "observed": current.observed_state,
                    "needs_restart": current.needs_restart,
                    "restart_count": current.restart_count,
                    "provider_session_persisted": (
                        current.provider_session_id is not None
                    ),
                    "recovery_file": current.recovery_file_path,
                    "recovery_state": current.recovery_state,
                    "last_recovery_error": current.last_recovery_error,
                    "tmux_session": current.tmux_session_name,
                    "project_path": current.project_path,
                }
            )
    print(json.dumps(rows, indent=2, sort_keys=True))


def worker_adopt_session_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        worker = store.resolve_detached_worker(args.name)
        if worker is None:
            raise StoreError("Detached worker was not found.")
        candidate = replace(worker, provider_session_id=args.session_id)
        if not detached_worker.validate_provider_session(candidate):
            raise StoreError(
                "That provider session was not found for the worker's exact "
                "working directory."
            )
        config = dict(worker.provider_config)
        if args.model:
            config["model"] = args.model
        if args.effort:
            config["effort"] = args.effort
        recovery_file = detached_worker.ensure_recovery_file(store, worker.name)
        updated = store.configure_detached_worker_recovery(
            worker.name,
            provider_session_id=args.session_id,
            provider_config=config,
            recovery_file_path=str(recovery_file),
            recovery_prompt=detached_worker.DEFAULT_RECOVERY_PROMPT,
        )
    print(
        json.dumps(
            {
                "name": updated.name,
                "provider": updated.provider,
                "provider_session_persisted": True,
                "recovery_file": updated.recovery_file_path,
            },
            indent=2,
            sort_keys=True,
        )
    )


def worker_recovery_confirm_command(args: argparse.Namespace) -> None:
    summary = sys.stdin.read().strip()
    with open_store(args.db) as store:
        worker = detached_worker.confirm_recovery(
            store,
            args.name,
            args.generation,
            summary,
        )
    print(
        f"Recovery generation {worker.recovery_generation} confirmed for "
        f"{worker.name}."
    )


def worker_recovery_fail_command(args: argparse.Namespace) -> None:
    reason = sys.stdin.read().strip()
    with open_store(args.db) as store:
        worker = detached_worker.fail_recovery(
            store,
            args.name,
            args.generation,
            reason,
        )
    print(
        f"Recovery generation {worker.recovery_generation} failed for "
        f"{worker.name}."
    )


def maintain_workers_once(
    store: DurableStore,
    *,
    now: Optional[float] = None,
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for worker in store.list_detached_workers():
        try:
            result, _ = detached_worker.recover_worker(
                store,
                worker.name,
                now=now,
            )
        except Exception as error:
            result = "error"
            log_event(
                "detached_worker_recovery_error",
                worker=worker.name,
                error=str(error),
            )
        counts[result] = counts.get(result, 0) + 1
    return counts


def maintain_workers_command(args: argparse.Namespace) -> None:
    """Recreate intended-running detached sessions after process loss."""
    with open_store(args.db) as store:
        while True:
            counts = maintain_workers_once(store)
            if counts and any(
                state not in {"running", "stopped", "awaiting_confirmation"}
                for state in counts
            ):
                log_event("detached_worker_maintenance_cycle", **counts)
            if args.once:
                return
            time.sleep(args.idle_sleep)


def worker_stop_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        worker = detached_worker.stop_worker(store, args.name)
        recovery_file_deleted = detached_worker.remove_recovery_file(store, worker)
        deleted_topic = False
        if args.delete_topic:
            chat_id, thread_id = detached_worker.resolve_destination(store, args.name)
            token = bridge.read_token()
            bridge.api_call(
                token,
                "deleteForumTopic",
                chat_id=chat_id,
                message_thread_id=thread_id,
            )
            store.retire_missing_topic(
                worker.binding_id,
                reason="Detached worker topic deleted at teardown.",
            )
            deleted_topic = True
        store.delete_detached_worker(args.name)
    print(
        json.dumps(
            {
                "name": worker.name,
                "stopped": True,
                "recovery_file_deleted": recovery_file_deleted,
                "topic_deleted": deleted_topic,
            },
            indent=2,
            sort_keys=True,
        )
    )


def install_skills_command(_: argparse.Namespace) -> None:
    installed = install_managed_skills()
    print("Installed shared Telegram Control skills:")
    for name in installed:
        print(f"- {name}")


def request_restart_command(args: argparse.Namespace) -> None:
    """Queue a restart for the supervisor to apply once the queues are quiet."""
    with open_store(args.db) as store:
        request = store.request_controller_restart(args.reason or "")
        busy = store.leased_work_counts()
    print("Restart queued.")
    if request["reason"]:
        print(f"Reason: {request['reason']}")
    if busy:
        print(
            "Waiting for active work: "
            + ", ".join(f"{table}={count}" for table, count in sorted(busy.items()))
        )
        print("The supervisor applies it as soon as nothing is leased.")
    else:
        print("Nothing is leased; the supervisor applies it within seconds.")


def registered_bot_commands() -> list[dict[str, str]]:
    """Render the help copy's command list for Telegram's own command menu."""
    commands = []
    for command in telegram_help.COMMANDS:
        name = command.command
        description = command.description
        if not re.fullmatch(r"[a-z0-9_]{1,32}", name):
            raise StoreError(f"Bot command name is invalid: {name}")
        if not 1 <= len(description) <= 256:
            raise StoreError(f"Bot command description is invalid: {name}")
        commands.append({"command": name, "description": description})
    return commands


def sync_commands_command(_: argparse.Namespace) -> None:
    """Publish the command menu so every command is tappable, pin or not."""
    token = bridge.read_token()
    commands = registered_bot_commands()
    bridge.api_call(token, "setMyCommands", commands=commands)
    print(f"Registered {len(commands)} Telegram commands:")
    for command in commands:
        print(f"- /{command['command']} — {command['description']}")


def install_command(args: argparse.Namespace) -> None:
    config = bridge.load_config()
    bridge.read_token()
    handler_path = Path(config["handler_path"]).expanduser().resolve()
    if not handler_path.is_file():
        raise StoreError(f"Handler does not exist: {handler_path}")
    bridge.handler_command(handler_path)
    with open_store(args.db):
        pass
    install_managed_skills()
    try:
        sync_commands_command(args)
    except bridge.BridgeError as exc:
        # A registered command menu is convenience, not a prerequisite; a
        # network hiccup here must not abort installing the controller.
        print(f"Warning: could not register Telegram commands: {exc}")

    bridge.LOG_DIR.mkdir(parents=True, exist_ok=True)
    bridge.PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    previous_plist: Optional[bytes]
    try:
        previous_plist = bridge.PLIST_PATH.read_bytes()
    except FileNotFoundError:
        previous_plist = None
    was_loaded = (
        bridge.launchctl(
            "print",
            f"{domain}/{bridge.LAUNCH_AGENT_LABEL}",
            check=False,
        ).returncode
        == 0
    )

    bridge.launchctl("bootout", domain, str(bridge.PLIST_PATH), check=False)
    write_plist(bridge.PLIST_PATH, launch_agent_plist(args.db))
    result = bridge.launchctl(
        "bootstrap",
        domain,
        str(bridge.PLIST_PATH),
        check=False,
    )
    if result.returncode != 0:
        bridge.launchctl("bootout", domain, str(bridge.PLIST_PATH), check=False)
        if previous_plist is None:
            bridge.PLIST_PATH.unlink(missing_ok=True)
        else:
            temporary_path = bridge.PLIST_PATH.with_suffix(".rollback.tmp")
            temporary_path.write_bytes(previous_plist)
            bridge.ensure_private_file(temporary_path)
            temporary_path.replace(bridge.PLIST_PATH)
            if was_loaded:
                bridge.launchctl(
                    "bootstrap",
                    domain,
                    str(bridge.PLIST_PATH),
                    check=False,
                )
        raise StoreError(
            result.stderr.strip()
            or "launchctl could not install the durable controller."
        )

    bridge.launchctl(
        "kickstart",
        "-k",
        f"{domain}/{bridge.LAUNCH_AGENT_LABEL}",
        check=False,
    )
    print("Durable Telegram controller installed and started.")
    print(f"Database: {args.db}")
    print(f"Logs: {bridge.LOG_DIR / 'telegram-control.log'}")
    print(f"Errors: {bridge.LOG_DIR / 'telegram-control.error.log'}")
    print(
        "Rollback: "
        f"{bridge.SCRIPT_PATH} install --handler {handler_path}"
    )


def restart_command(args: argparse.Namespace) -> None:
    """Schedule exactly one controller restart from a self-cleaning job."""
    delay = float(args.delay_seconds)
    database_path = Path(getattr(args, "db", DATABASE_PATH))
    if delay < 1.0 or delay > MAX_RESTART_DELAY_SECONDS:
        raise StoreError(
            "Restart delay must be between 1 and "
            f"{int(MAX_RESTART_DELAY_SECONDS)} seconds."
        )
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{bridge.LAUNCH_AGENT_LABEL}"
    loaded = bridge.launchctl("print", service, check=False)
    if loaded.returncode != 0:
        raise StoreError("The durable Telegram controller is not loaded.")

    cleanup_reload_jobs()
    label = f"{RELOAD_JOB_PREFIX}.{os.getpid()}.{uuid.uuid4().hex[:8]}"
    log_path = Path("/tmp") / f"{label}.log"
    restart_if_idle = " ".join(
        shlex.quote(part)
        for part in (
            sys.executable,
            str(SCRIPT_PATH),
            "--db",
            str(database_path),
            "restart-if-idle",
        )
    )
    # The helper waits until every durable worker lease is idle. The hidden
    # command holds SQLite's write lock while it kickstarts launchd, closing
    # the race where a worker could claim a turn between an idle check and
    # the restart. run_command() removes the one-shot helper after replacement.
    script = (
        f"sleep {delay:.3f}; "
        f"until {restart_if_idle}; do sleep 1; done; "
        "status=$?; "
        f"/bin/launchctl remove {label}; "
        "exit $status"
    )
    submitted = bridge.launchctl(
        "submit",
        "-l",
        label,
        "-o",
        str(log_path),
        "-e",
        str(log_path),
        "--",
        "/bin/sh",
        "-c",
        script,
        check=False,
    )
    if submitted.returncode != 0:
        raise StoreError(
            submitted.stderr.strip()
            or "launchctl could not schedule the controller restart."
        )
    print(
        "Scheduled one Telegram controller restart in "
        f"{delay:g} seconds; active durable work will finish first."
    )
    print(f"Reload job: {label}")


def restart_if_idle_command(args: argparse.Namespace) -> None:
    """Atomically restart launchd only when no durable worker owns a lease."""
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{bridge.LAUNCH_AGENT_LABEL}"
    with DurableStore(args.db) as store:
        store.connection.execute("BEGIN IMMEDIATE")
        try:
            timestamp = time.time()
            orphaned_agent_ids = set()
            orphaned_mailbox_ids = []
            local_host = socket.gethostname()
            leased_agents = store.connection.execute(
                """
                SELECT mailbox_id, agent_id, lease_owner, provider_turn_id
                FROM agent_mailbox
                WHERE state = 'leased'
                """
            ).fetchall()
            for row in leased_agents:
                if row["provider_turn_id"] is not None:
                    continue
                owner = str(row["lease_owner"] or "")
                match = re.fullmatch(
                    r"(.+):([1-9][0-9]*):agent:[A-Za-z0-9_-]+",
                    owner,
                )
                if match is None or match.group(1) != local_host:
                    continue
                owner_pid = int(match.group(2))
                try:
                    os.kill(owner_pid, 0)
                except ProcessLookupError:
                    orphaned_mailbox_ids.append(int(row["mailbox_id"]))
                    orphaned_agent_ids.add(str(row["agent_id"]))
                except PermissionError:
                    pass
            for mailbox_id in orphaned_mailbox_ids:
                store.connection.execute(
                    """
                    UPDATE agent_mailbox
                    SET state = 'queued', available_at = ?,
                        lease_owner = NULL, lease_expires_at = NULL,
                        updated_at = ?
                    WHERE mailbox_id = ? AND state = 'leased'
                        AND provider_turn_id IS NULL
                    """,
                    (timestamp, timestamp, mailbox_id),
                )
            for agent_id in orphaned_agent_ids:
                store.connection.execute(
                    """
                    UPDATE agents
                    SET lifecycle_state = 'registered', updated_at = ?
                    WHERE agent_id = ? AND lifecycle_state = 'running'
                    """,
                    (timestamp, agent_id),
                )
            active = {}
            for table in (
                "inbox_jobs",
                "router_mailbox",
                "agent_mailbox",
                "outbox_messages",
            ):
                count = int(
                    store.connection.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE state = 'leased'"
                    ).fetchone()[0]
                )
                if count:
                    active[table] = count
            if active:
                raise StoreError(
                    "Controller restart is waiting for active durable work: "
                    + ", ".join(
                        f"{table}={count}"
                        for table, count in sorted(active.items())
                    )
                )
            if orphaned_mailbox_ids:
                log_event(
                    "restart_recovered_prestart_agent_leases",
                    mailbox_ids=orphaned_mailbox_ids,
                )
            restarted = bridge.launchctl(
                "kickstart",
                "-k",
                service,
                check=False,
            )
            if restarted.returncode != 0:
                raise StoreError(
                    restarted.stderr.strip()
                    or "launchctl could not restart the durable controller."
                )
            store.connection.execute("COMMIT")
        except BaseException:
            if store.connection.in_transaction:
                store.connection.execute("ROLLBACK")
            raise


def init_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        print(f"Initialized schema {SCHEMA_VERSION} at {store.path}")
        print(f"Database check: {store.quick_check()}")
        print(f"Polling offset: {store.poll_offset()}")


def status_command(args: argparse.Namespace) -> None:
    domain = f"gui/{os.getuid()}"
    launch_result = bridge.launchctl(
        "print",
        f"{domain}/{bridge.LAUNCH_AGENT_LABEL}",
        check=False,
    )
    with open_store(args.db) as store:
        router_metrics = store.router_session_metrics()
        status = {
            "database": str(store.path),
            "quick_check": store.quick_check(),
            "poll_offset": store.poll_offset(),
            "launch_agent": {
                "configured_mode": configured_launch_agent_mode(),
                "loaded": launch_result.returncode == 0,
            },
            "router_session": {
                "persisted": router_metrics["provider_session_id"] is not None,
                "completed_turns": router_metrics["completed_turns"],
                "input_tokens": router_metrics["input_tokens"],
                "turn_limit": ROUTER_MAX_COMPLETED_TURNS,
                "input_token_limit": ROUTER_MAX_INPUT_TOKENS,
                "rotations": store.router_rotation_count(),
            },
            "queues": store.status_counts(),
        }
        pending_restart = store.pending_restart_request()
        if pending_restart is not None:
            status["pending_restart"] = {
                **pending_restart,
                "blocked_by": store.leased_work_counts(),
            }
    print(json.dumps(status, indent=2, sort_keys=True))


def topic_capability_command(_: argparse.Namespace) -> None:
    bot = bridge.api_call(bridge.read_token(), "getMe")
    capability = {
        "username": bot.get("username"),
        "has_topics_enabled": bool(bot.get("has_topics_enabled", False)),
        "allows_users_to_create_topics": bool(
            bot.get("allows_users_to_create_topics", False)
        ),
    }
    print(json.dumps(capability, indent=2, sort_keys=True))


def provision_topic_command(args: argparse.Namespace) -> None:
    name = str(args.name).strip()
    if not name or len(name) > 128:
        raise StoreError("Topic name must contain between 1 and 128 characters.")
    config = bridge.load_config()
    chat_id = int(config["chat_id"])
    with open_store(args.db) as store:
        existing = store.resolve_named_surface(
            chat_id,
            name,
            surface_type=args.surface_type,
        )
        if existing is not None:
            result = {
                "created": False,
                "binding_id": existing.binding_id,
                "chat_id": existing.chat_id,
                "message_thread_id": existing.message_thread_id,
                "display_name": existing.display_name,
                "target": f"{existing.target_type}/{existing.target_id}",
            }
        else:
            token = bridge.read_token()
            bot = bridge.api_call(token, "getMe")
            if not bool(bot.get("has_topics_enabled", False)):
                raise StoreError(
                    "Telegram Threaded Mode is disabled for this bot. "
                    "Enable it in BotFather first."
                )
            topic = bridge.api_call(
                token,
                "createForumTopic",
                chat_id=chat_id,
                name=name,
            )
            try:
                message_thread_id = int(topic["message_thread_id"])
            except (KeyError, TypeError, ValueError):
                raise StoreError(
                    "Telegram returned an invalid forum-topic result."
                ) from None
            binding = store.ensure_surface_binding(
                chat_id=chat_id,
                message_thread_id=message_thread_id,
                surface_type=args.surface_type,
                display_name=name,
                target_type=args.target_type,
                target_id=args.target_id,
            )
            result = {
                "created": True,
                "binding_id": binding.binding_id,
                "chat_id": binding.chat_id,
                "message_thread_id": binding.message_thread_id,
                "display_name": binding.display_name,
                "target": f"{binding.target_type}/{binding.target_id}",
            }
    print(json.dumps(result, indent=2, sort_keys=True))


def register_agent_command(args: argparse.Namespace) -> None:
    requested_path = Path(args.project_path).expanduser().resolve()
    if not requested_path.is_dir():
        raise StoreError(f"Workspace directory does not exist: {requested_path}")
    project_path, working_directory, _ = discovery.validate_agent_workspace(
        str(requested_path)
    )
    git_repository_root = discovery.exact_git_root(project_path)
    config = bridge.load_config()
    with open_store(args.db) as store:
        agent, created = store.register_project_agent(
            chat_id=int(config["chat_id"]),
            surface_name=args.surface,
            slug=args.slug,
            provider=args.provider,
            project_path=project_path,
            working_directory=working_directory,
            git_repository_root=git_repository_root,
        )
    print(
        json.dumps(
            {
                "created": created,
                "agent_id": agent.agent_id,
                "name": agent.hierarchical_name,
                "provider": agent.provider,
                "project_path": agent.project_path,
                "state": agent.lifecycle_state,
                "surface_binding_id": agent.surface_binding_id,
            },
            indent=2,
            sort_keys=True,
        )
    )


def enroll_project_command(args: argparse.Namespace) -> None:
    requested_path = Path(args.project_path).expanduser().resolve()
    if not requested_path.is_dir():
        raise StoreError(f"Workspace directory does not exist: {requested_path}")
    project_path, working_directory, _ = discovery.validate_agent_workspace(
        str(requested_path)
    )
    git_repository_root = discovery.exact_git_root(project_path)
    display_name = args.name or args.slug.replace("-", " ").title()
    with open_store(args.db) as store:
        project, created = store.enroll_project(
            slug=args.slug,
            display_name=display_name,
            provider=args.provider,
            project_path=project_path,
            working_directory=working_directory,
            git_repository_root=git_repository_root,
        )
    print(
        json.dumps(
            {
                "created": created,
                "project_id": project.project_id,
                "slug": project.slug,
                "display_name": project.display_name,
                "provider": project.provider,
                "project_path": project.project_path,
            },
            indent=2,
            sort_keys=True,
        )
    )


def resolve_cli_agent(store: DurableStore, name: str):
    agent = store.resolve_agent_by_name(name)
    if agent is None or agent.role not in {"project", "worker"}:
        raise StoreError(f"Managed agent was not found: {name}")
    return agent


def console_open_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        agent = resolve_cli_agent(store, args.agent)
        console = tmux_console.open_agent_console(store, agent)
    print(f"Managed console running: {console.tmux_session_name}")
    print(f"Attach: tmux attach-session -t '={console.tmux_session_name}'")


def console_close_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        agent = resolve_cli_agent(store, args.agent)
        console = tmux_console.close_agent_console(store, agent)
    print(f"Managed console stopped: {console.tmux_session_name}")


def console_status_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        agent = resolve_cli_agent(store, args.agent)
        console = tmux_console.reconcile_agent_console(store, agent.agent_id)
    state = console.state if console is not None else "not created"
    session_name = (
        console.tmux_session_name if console is not None else agent.hierarchical_name
    )
    print(
        json.dumps(
            {
                "agent": agent.hierarchical_name,
                "console": state,
                "tmux_session": session_name,
            },
            indent=2,
            sort_keys=True,
        )
    )


def doctor_command(args: argparse.Namespace) -> None:
    problems = []
    try:
        config = bridge.load_config()
        print(f"Pairing: chat {config['chat_id']}")
    except bridge.BridgeError as exc:
        problems.append(str(exc))
    try:
        bridge.read_token()
        print("Keychain token: available")
    except bridge.BridgeError as exc:
        problems.append(str(exc))
    try:
        with open_store(args.db) as store:
            check = store.quick_check()
            if check != "ok":
                problems.append(f"SQLite quick_check returned: {check}")
            else:
                print(f"Database: ok ({store.path})")
    except (OSError, sqlite3.Error, StoreError) as exc:
        problems.append(str(exc))
    if problems:
        for problem in problems:
            print(f"Problem: {problem}", file=sys.stderr)
        raise StoreError(f"Doctor found {len(problems)} problem(s).")
    print("Doctor: all Stage 1 prerequisites passed.")


def retry_command(args: argparse.Namespace) -> None:
    with open_store(args.db) as store:
        count = store.retry_dead(args.queue)
    print(f"Requeued {count} dead {args.queue} item(s).")


def add_loop_arguments(parser: argparse.ArgumentParser, lease_seconds: float) -> None:
    parser.add_argument("--once", action="store_true", help="Process at most one item.")
    parser.add_argument(
        "--lease-seconds",
        type=float,
        default=lease_seconds,
        help="Seconds before a crashed worker's lease can be recovered.",
    )
    parser.add_argument("--idle-sleep", type=float, default=0.5)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable Telegram transport controller.")
    parser.add_argument(
        "--db",
        type=Path,
        default=DATABASE_PATH,
        help=f"SQLite database path (default: {DATABASE_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Initialize and verify the database.")
    init_parser.set_defaults(function=init_command)

    collect_parser = subparsers.add_parser(
        "collect", help="Durably collect Telegram updates."
    )
    collect_parser.add_argument(
        "--once", action="store_true", help="Make one non-blocking Telegram request."
    )
    collect_parser.set_defaults(function=collect_command)

    work_parser = subparsers.add_parser("work", help="Process durable inbox jobs.")
    add_loop_arguments(work_parser, lease_seconds=20 * 60)
    work_parser.set_defaults(function=work_command)

    agent_work_parser = subparsers.add_parser(
        "work-agents",
        help="Process serialized managed-agent mailbox items.",
    )
    add_loop_arguments(agent_work_parser, lease_seconds=2 * 60 * 60)
    agent_work_parser.set_defaults(function=work_agents_command)

    router_work_parser = subparsers.add_parser(
        "work-router",
        help="Process serialized main-router mailbox items.",
    )
    add_loop_arguments(router_work_parser, lease_seconds=10 * 60)
    router_work_parser.set_defaults(function=work_router_command)

    outbox_parser = subparsers.add_parser(
        "send-outbox", help="Deliver durable Telegram API calls."
    )
    add_loop_arguments(outbox_parser, lease_seconds=90)
    outbox_parser.set_defaults(function=send_outbox_command)

    topic_maintenance_parser = subparsers.add_parser(
        "maintain-topics",
        help="Retire controller routes for topics deleted directly in Telegram.",
    )
    topic_maintenance_parser.add_argument(
        "--once",
        action="store_true",
        help="Probe one due batch and exit.",
    )
    topic_maintenance_parser.add_argument(
        "--interval-seconds",
        type=float,
        default=TOPIC_PROBE_INTERVAL_SECONDS,
        help="Minimum seconds between existence checks for a topic.",
    )
    topic_maintenance_parser.add_argument(
        "--batch-size",
        type=int,
        choices=range(1, 1001),
        default=TOPIC_PROBE_BATCH_SIZE,
        help="Maximum topics checked in one maintenance cycle.",
    )
    topic_maintenance_parser.add_argument(
        "--idle-sleep",
        type=float,
        default=5 * 60,
        help="Seconds between checks for newly due topics.",
    )
    topic_maintenance_parser.set_defaults(function=maintain_topics_command)

    worker_maintenance_parser = subparsers.add_parser(
        "maintain-workers",
        help="Reconcile and recover intended-running detached workers.",
    )
    worker_maintenance_parser.add_argument(
        "--once",
        action="store_true",
        help="Run one detached-worker reconciliation cycle and exit.",
    )
    worker_maintenance_parser.add_argument(
        "--idle-sleep",
        type=float,
        default=15,
        help="Seconds between detached-worker reconciliation cycles.",
    )
    worker_maintenance_parser.set_defaults(function=maintain_workers_command)

    run_parser = subparsers.add_parser(
        "run", help="Run collector, inbox worker, and outbox sender."
    )
    run_parser.add_argument(
        "--agent-workers",
        type=int,
        choices=range(1, MAX_AGENT_WORKERS + 1),
        default=DEFAULT_AGENT_WORKERS,
        help=(
            "Managed-agent turns to run concurrently across distinct agents "
            f"(default: {DEFAULT_AGENT_WORKERS})."
        ),
    )
    run_parser.set_defaults(function=run_command)

    install_parser = subparsers.add_parser(
        "install", help="Replace Stage 0 with the durable background controller."
    )
    install_parser.set_defaults(function=install_command)

    restart_parser = subparsers.add_parser(
        "restart",
        help="Schedule one guarded background-controller restart.",
    )
    restart_parser.add_argument(
        "--delay-seconds",
        type=float,
        default=DEFAULT_RESTART_DELAY_SECONDS,
        help=(
            "Seconds to wait before restarting, allowing the current response "
            f"to finish (default: {DEFAULT_RESTART_DELAY_SECONDS:g})."
        ),
    )
    restart_parser.set_defaults(function=restart_command)

    request_restart_parser = subparsers.add_parser(
        "request-restart",
        help="Queue a restart the supervisor applies once nothing is leased.",
    )
    request_restart_parser.add_argument(
        "--reason",
        default="",
        help="Short note recorded with the request, shown in status and logs.",
    )
    request_restart_parser.set_defaults(function=request_restart_command)

    restart_if_idle_parser = subparsers.add_parser(
        "restart-if-idle",
        help=argparse.SUPPRESS,
    )
    restart_if_idle_parser.set_defaults(function=restart_if_idle_command)

    worker_start_parser = subparsers.add_parser(
        "worker-start",
        help="Start a detached tmux worker with its own report-only topic.",
    )
    worker_start_parser.add_argument("name", help="Lowercase worker slug.")
    worker_start_parser.add_argument(
        "--provider", choices=("codex", "claude"), default="claude"
    )
    worker_start_parser.add_argument("--model", default=None)
    worker_start_parser.add_argument("--effort", default=None)
    worker_start_parser.add_argument("--project-path", default=None)
    worker_start_parser.set_defaults(function=worker_start_command)

    worker_brief_parser = subparsers.add_parser(
        "worker-brief",
        help="Send a task brief from a file into a detached worker.",
    )
    worker_brief_parser.add_argument("name")
    worker_brief_parser.add_argument("--file", required=True)
    worker_brief_parser.set_defaults(function=worker_brief_command)

    worker_report_parser = subparsers.add_parser(
        "worker-report",
        help="Post an update from a detached worker into its topic (stdin).",
    )
    worker_report_parser.add_argument("name")
    worker_report_parser.add_argument("--key", required=True)
    worker_report_parser.add_argument("--text", action="store_true")
    worker_report_parser.set_defaults(function=worker_report_command)

    worker_status_parser = subparsers.add_parser(
        "worker-status", help="Show detached worker intent versus reality."
    )
    worker_status_parser.set_defaults(function=worker_status_command)

    worker_adopt_parser = subparsers.add_parser(
        "worker-adopt-session",
        help="Attach an existing provider session to a detached worker.",
    )
    worker_adopt_parser.add_argument("name")
    worker_adopt_parser.add_argument("--session-id", required=True)
    worker_adopt_parser.add_argument("--model", default=None)
    worker_adopt_parser.add_argument("--effort", default=None)
    worker_adopt_parser.set_defaults(function=worker_adopt_session_command)

    worker_recovery_confirm_parser = subparsers.add_parser(
        "worker-recovery-confirm",
        help="Confirm that a recovered worker restored its state (stdin summary).",
    )
    worker_recovery_confirm_parser.add_argument("name")
    worker_recovery_confirm_parser.add_argument(
        "--generation",
        type=int,
        required=True,
    )
    worker_recovery_confirm_parser.set_defaults(
        function=worker_recovery_confirm_command
    )

    worker_recovery_fail_parser = subparsers.add_parser(
        "worker-recovery-fail",
        help="Report that a recovered worker could not restore state (stdin reason).",
    )
    worker_recovery_fail_parser.add_argument("name")
    worker_recovery_fail_parser.add_argument(
        "--generation",
        type=int,
        required=True,
    )
    worker_recovery_fail_parser.set_defaults(function=worker_recovery_fail_command)

    worker_stop_parser = subparsers.add_parser(
        "worker-stop", help="Stop a detached worker and optionally delete its topic."
    )
    worker_stop_parser.add_argument("name")
    worker_stop_parser.add_argument("--delete-topic", action="store_true")
    worker_stop_parser.set_defaults(function=worker_stop_command)

    install_skills_parser = subparsers.add_parser(
        "install-skills",
        help="Install repo-owned skills for Codex and Claude discovery.",
    )
    install_skills_parser.set_defaults(function=install_skills_command)

    sync_commands_parser = subparsers.add_parser(
        "sync-commands",
        help="Register the Telegram command menu from the help copy.",
    )
    sync_commands_parser.set_defaults(function=sync_commands_command)

    status_parser = subparsers.add_parser("status", help="Show durable queue status.")
    status_parser.set_defaults(function=status_command)

    topic_parser = subparsers.add_parser(
        "topic-capability",
        help="Show whether Telegram private-chat topics are enabled for this bot.",
    )
    topic_parser.set_defaults(function=topic_capability_command)

    provision_parser = subparsers.add_parser(
        "provision-topic",
        help="Create and bind one managed private-chat topic.",
    )
    provision_parser.add_argument("name", help="Telegram topic and surface name.")
    provision_parser.add_argument(
        "--surface-type",
        choices=("project", "task"),
        default="project",
    )
    provision_parser.add_argument("--target-type", default="controller")
    provision_parser.add_argument("--target-id", default="control")
    provision_parser.set_defaults(function=provision_topic_command)

    register_parser = subparsers.add_parser(
        "register-agent",
        help="Register a managed project agent on an existing topic surface.",
    )
    register_parser.add_argument("surface", help="Existing managed topic name.")
    register_parser.add_argument("slug", help="Lowercase hierarchical agent slug.")
    register_parser.add_argument("project_path", help="Local workspace directory.")
    register_parser.add_argument(
        "--provider",
        choices=("codex", "claude"),
        default="codex",
    )
    register_parser.set_defaults(function=register_agent_command)

    enroll_parser = subparsers.add_parser(
        "enroll-project",
        help="Enroll a validated local workspace for Telegram selection.",
    )
    enroll_parser.add_argument("slug", help="Stable lowercase project slug.")
    enroll_parser.add_argument("project_path", help="Local workspace directory.")
    enroll_parser.add_argument("--name", help="User-facing project name.")
    enroll_parser.add_argument(
        "--provider",
        choices=("codex", "claude"),
        default="codex",
    )
    enroll_parser.set_defaults(function=enroll_project_command)

    console_open_parser = subparsers.add_parser(
        "console-open",
        help="Open an explicit tmux takeover for a persisted agent session.",
    )
    console_open_parser.add_argument("agent", help="Hierarchical managed-agent name.")
    console_open_parser.set_defaults(function=console_open_command)

    console_close_parser = subparsers.add_parser(
        "console-close",
        help="Close a managed tmux console and resume mailbox processing.",
    )
    console_close_parser.add_argument("agent", help="Hierarchical managed-agent name.")
    console_close_parser.set_defaults(function=console_close_command)

    console_status_parser = subparsers.add_parser(
        "console-status",
        help="Show and reconcile a managed tmux console.",
    )
    console_status_parser.add_argument("agent", help="Hierarchical managed-agent name.")
    console_status_parser.set_defaults(function=console_status_command)

    doctor_parser = subparsers.add_parser("doctor", help="Check Stage 1 prerequisites.")
    doctor_parser.set_defaults(function=doctor_command)

    retry_parser = subparsers.add_parser("retry", help="Requeue dead items.")
    retry_parser.add_argument(
        "queue",
        choices=("inbox", "outbox", "agent", "router"),
    )
    retry_parser.set_defaults(function=retry_command)
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
    except (bridge.BridgeError, sqlite3.Error, StoreError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
